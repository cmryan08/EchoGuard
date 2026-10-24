/*
 * SPDX-FileCopyrightText: 2025-2026 Espressif Systems (Shanghai) CO LTD
 *
 * SPDX-License-Identifier: Apache-2.0
 */
/* EchoGuard CSI receiver

   Derived from espressif/esp-csi examples/get-started/csi_recv (Public Domain /
   CC0 example code). Kept unchanged: wifi_init(), ESP-NOW init, per-target
   csi_config, sender MAC filter, esp_csi_gain_ctrl logic, and the official
   CSI_DATA serial line formats (classic 25-field and C5/C6 15-field).

   EchoGuard changes:
     (a) No printing inside the CSI callback. The callback only copies rx_ctrl,
         mac, id, first_word_invalid, len, compensate_gain and up to
         ECHOGUARD_RX_MAX_CSI_BYTES of info->buf into a pre-allocated queue item
         and calls xQueueSend(..., 0). If the queue is full the packet is
         dropped and counted. A separate printer task formats the line.
     (b) The per-packet ESP_LOGI("compensate_gain ...") of the official
         callback is removed (it alone would flood the serial link).
     (c) ECHOGUARD_RX_LLTF_ONLY (default y, classic targets only) prints only
         the first 128 values (64 LLTF subcarriers) and sets 'len' accordingly.
     (d) Classic line: the 'rx_state' column prints rx_ctrl->rx_state (the
         official code passes sig_mode a second time there).
     (e) Once per second: EG_RX_STATS,<uptime_ms>,<csi_rx_total>,
         <csi_rx_rate_hz>,<queue_drops>,<seq_gaps>,<last_rssi>
     (f) Startup banner (target, channel, expected TX MAC, LLTF-only flag).
     Channel comes from Kconfig ECHOGUARD_RX_CHANNEL (must match transmitter).

   Serial bandwidth (921600 baud UART, 8N1 = 92160 bytes/s):
     One CSI value prints as up to 4 chars plus a comma (e.g. "-12,"), on
     average ~3.5-4 bytes. Metadata prefix is ~100 bytes.
       LLTF only : 128 values * ~4 B + ~100 B  ~=  600 B/line
                   * 100 Hz                    ~=   60 kB/s  -> fits (~65 %)
       HT40 full : 384 values * ~4 B + ~100 B  ~= 1640 B/line
                   * 100 Hz                    ~=  164 kB/s  -> does NOT fit
     Hence LLTF-only is the default on classic targets.

   Unless required by applicable law or agreed to in writing, this
   software is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR
   CONDITIONS OF ANY KIND, either express or implied.
*/

#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include <stdbool.h>
#include <inttypes.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"

#include "nvs_flash.h"

#include "esp_mac.h"
#include "rom/ets_sys.h"
#include "esp_log.h"
#include "esp_wifi.h"
#include "esp_netif.h"
#include "esp_now.h"
#include "esp_timer.h"
#include "esp_idf_version.h"
#include "esp_csi_gain_ctrl.h"

#define CONFIG_LESS_INTERFERENCE_CHANNEL   CONFIG_ECHOGUARD_RX_CHANNEL

/* HT40 needs the secondary 20 MHz channel inside 1..13: "below" for 5..13
 * (official default for channel 11), "above" for 1..4. Same rule as the TX. */
#define EG_SECOND_CHAN ((CONFIG_LESS_INTERFERENCE_CHANNEL >= 5) ? WIFI_SECOND_CHAN_BELOW : WIFI_SECOND_CHAN_ABOVE)

#if CONFIG_IDF_TARGET_ESP32C5 || CONFIG_IDF_TARGET_ESP32C61 || (CONFIG_IDF_TARGET_ESP32C6 && ESP_IDF_VERSION >= ESP_IDF_VERSION_VAL(5, 4, 0))
#define CONFIG_WIFI_BAND_MODE               WIFI_BAND_MODE_2G_ONLY
#define CONFIG_WIFI_2G_BANDWIDTHS           WIFI_BW_HT40
#define CONFIG_WIFI_5G_BANDWIDTHS           WIFI_BW_HT40
#define CONFIG_WIFI_2G_PROTOCOL             WIFI_PROTOCOL_11N
#define CONFIG_WIFI_5G_PROTOCOL             WIFI_PROTOCOL_11N
#else
#define CONFIG_WIFI_BANDWIDTH           WIFI_BW_HT40
#endif

#define CONFIG_ESP_NOW_PHYMODE           WIFI_PHY_MODE_HT40
#define CONFIG_ESP_NOW_RATE             WIFI_PHY_RATE_MCS0_LGI
#define CONFIG_FORCE_GAIN                   0

#if CONFIG_IDF_TARGET_ESP32C5 || CONFIG_IDF_TARGET_ESP32C61
#define CSI_FORCE_LLTF                      0
#endif

#if CONFIG_IDF_TARGET_ESP32S3 || CONFIG_IDF_TARGET_ESP32C3 || CONFIG_IDF_TARGET_ESP32C5 || CONFIG_IDF_TARGET_ESP32C6 || CONFIG_IDF_TARGET_ESP32C61
#define CONFIG_GAIN_CONTROL                 1
#endif

#if CONFIG_IDF_TARGET_ESP32C5 || CONFIG_IDF_TARGET_ESP32C6 || CONFIG_IDF_TARGET_ESP32C61
#define EG_C5C6_FORMAT                      1
#else
#define EG_C5C6_FORMAT                      0
#endif

#if ESP_IDF_VERSION >= ESP_IDF_VERSION_VAL(6, 0, 0)
#define ESP_IF_WIFI_STA ESP_MAC_WIFI_STA
#endif

/* Number of int8 values printed per packet at most. */
#if CONFIG_ECHOGUARD_RX_LLTF_ONLY
#define EG_LLTF_VALUES                      128
#define EG_COPY_MAX                         ((CONFIG_ECHOGUARD_RX_MAX_CSI_BYTES < EG_LLTF_VALUES) ? CONFIG_ECHOGUARD_RX_MAX_CSI_BYTES : EG_LLTF_VALUES)
#else
#define EG_COPY_MAX                         CONFIG_ECHOGUARD_RX_MAX_CSI_BYTES
#endif

/* Worst case line: ~200 bytes metadata + 6 bytes per value ("-1234,"). */
#define EG_LINE_BUF_SIZE                    (256 + 6 * EG_COPY_MAX)

static const uint8_t CONFIG_CSI_SEND_MAC[] = {0x1a, 0x00, 0x00, 0x00, 0x00, 0x00};
static const char *TAG = "csi_recv";

/* One CSI packet as handed from the Wi-Fi callback to the printer task. */
typedef struct {
    wifi_pkt_rx_ctrl_t rx_ctrl;   /* full copy: cheap, avoids per-target field lists */
    uint8_t mac[6];
    uint32_t rx_id;
    float compensate_gain;
    uint8_t agc_gain;
    int8_t fft_gain;
    bool first_word_invalid;
    uint16_t len;                 /* number of bytes copied into buf */
    int8_t buf[EG_COPY_MAX];
} eg_csi_item_t;

static QueueHandle_t s_csi_queue = NULL;
/* The CSI callback runs in the Wi-Fi task only, so a single static staging item
 * is safe and keeps the large struct off the Wi-Fi task stack. */
static eg_csi_item_t s_cb_item;
static eg_csi_item_t s_print_item;
static char s_line[EG_LINE_BUF_SIZE];

/* Counters updated in the callback (single writer), read by the printer task. */
static volatile uint32_t s_rx_total = 0;
static volatile uint32_t s_queue_drops = 0;
static volatile uint32_t s_seq_gaps = 0;
static volatile int32_t s_last_rssi = 0;

static void wifi_init()
{
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    ESP_ERROR_CHECK(esp_netif_init());
    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_storage(WIFI_STORAGE_RAM));

#if CONFIG_IDF_TARGET_ESP32C5
    ESP_ERROR_CHECK(esp_wifi_start());
    esp_wifi_set_band_mode(CONFIG_WIFI_BAND_MODE);
    wifi_protocols_t protocols = {
        .ghz_2g = CONFIG_WIFI_2G_PROTOCOL,
        .ghz_5g = CONFIG_WIFI_5G_PROTOCOL
    };
    ESP_ERROR_CHECK(esp_wifi_set_protocols(ESP_IF_WIFI_STA, &protocols));
    wifi_bandwidths_t bandwidth = {
        .ghz_2g = CONFIG_WIFI_2G_BANDWIDTHS,
        .ghz_5g = CONFIG_WIFI_5G_BANDWIDTHS
    };
    ESP_ERROR_CHECK(esp_wifi_set_bandwidths(ESP_IF_WIFI_STA, &bandwidth));
#elif (CONFIG_IDF_TARGET_ESP32C6 && ESP_IDF_VERSION >= ESP_IDF_VERSION_VAL(5, 4, 0)) || CONFIG_IDF_TARGET_ESP32C61
    ESP_ERROR_CHECK(esp_wifi_start());
    esp_wifi_set_band_mode(CONFIG_WIFI_BAND_MODE);
    wifi_protocols_t protocols = {
        .ghz_2g = CONFIG_WIFI_2G_PROTOCOL,
    };
    ESP_ERROR_CHECK(esp_wifi_set_protocols(ESP_IF_WIFI_STA, &protocols));
    wifi_bandwidths_t bandwidth = {
        .ghz_2g = CONFIG_WIFI_2G_BANDWIDTHS,
    };
    ESP_ERROR_CHECK(esp_wifi_set_bandwidths(ESP_IF_WIFI_STA, &bandwidth));
#else
    ESP_ERROR_CHECK(esp_wifi_set_bandwidth(ESP_IF_WIFI_STA, CONFIG_WIFI_BANDWIDTH));
    ESP_ERROR_CHECK(esp_wifi_start());
#endif

    ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_NONE));
#if CONFIG_IDF_TARGET_ESP32C5
    if ((CONFIG_WIFI_BAND_MODE == WIFI_BAND_MODE_2G_ONLY && CONFIG_WIFI_2G_BANDWIDTHS == WIFI_BW_HT20)
            || (CONFIG_WIFI_BAND_MODE == WIFI_BAND_MODE_5G_ONLY && CONFIG_WIFI_5G_BANDWIDTHS == WIFI_BW_HT20)) {
        ESP_ERROR_CHECK(esp_wifi_set_channel(CONFIG_LESS_INTERFERENCE_CHANNEL, WIFI_SECOND_CHAN_NONE));
    } else {
        ESP_ERROR_CHECK(esp_wifi_set_channel(CONFIG_LESS_INTERFERENCE_CHANNEL, EG_SECOND_CHAN));
    }
#elif (CONFIG_IDF_TARGET_ESP32C6 && ESP_IDF_VERSION >= ESP_IDF_VERSION_VAL(5, 4, 0)) || CONFIG_IDF_TARGET_ESP32C61
    if (CONFIG_WIFI_BAND_MODE == WIFI_BAND_MODE_2G_ONLY && CONFIG_WIFI_2G_BANDWIDTHS == WIFI_BW_HT20) {
        ESP_ERROR_CHECK(esp_wifi_set_channel(CONFIG_LESS_INTERFERENCE_CHANNEL, WIFI_SECOND_CHAN_NONE));
    } else {
        ESP_ERROR_CHECK(esp_wifi_set_channel(CONFIG_LESS_INTERFERENCE_CHANNEL, EG_SECOND_CHAN));
    }
#else
    if (CONFIG_WIFI_BANDWIDTH == WIFI_BW_HT20) {
        ESP_ERROR_CHECK(esp_wifi_set_channel(CONFIG_LESS_INTERFERENCE_CHANNEL, WIFI_SECOND_CHAN_NONE));
    } else {
        ESP_ERROR_CHECK(esp_wifi_set_channel(CONFIG_LESS_INTERFERENCE_CHANNEL, EG_SECOND_CHAN));
    }
#endif

    /* Official example behaviour, kept unchanged. */
    ESP_ERROR_CHECK(esp_wifi_set_mac(WIFI_IF_STA, CONFIG_CSI_SEND_MAC));
}

static void wifi_esp_now_init(esp_now_peer_info_t peer)
{
    ESP_ERROR_CHECK(esp_now_init());
    ESP_ERROR_CHECK(esp_now_set_pmk((uint8_t *)"pmk1234567890123"));
    esp_now_rate_config_t rate_config = {
        .phymode = CONFIG_ESP_NOW_PHYMODE,
        .rate = CONFIG_ESP_NOW_RATE,//  WIFI_PHY_RATE_MCS0_LGI,
        .ersu = false,
        .dcm = false
    };
    ESP_ERROR_CHECK(esp_now_add_peer(&peer));
    ESP_ERROR_CHECK(esp_now_set_peer_rate_config(peer.peer_addr, &rate_config));

}

/* Runs in the Wi-Fi task. Keep it short: copy, enqueue, count. */
static void wifi_csi_rx_cb(void *ctx, wifi_csi_info_t *info)
{
    if (!info || !info->buf) {
        ESP_LOGW(TAG, "<%s> wifi_csi_cb", esp_err_to_name(ESP_ERR_INVALID_ARG));
        return;
    }

    if (memcmp(info->mac, CONFIG_CSI_SEND_MAC, 6)) {
        return;
    }

    const wifi_pkt_rx_ctrl_t *rx_ctrl = &info->rx_ctrl;
    static uint32_t s_count = 0;
    float compensate_gain = 1.0f;
    static uint8_t agc_gain = 0;
    static int8_t fft_gain = 0;
#if CONFIG_GAIN_CONTROL
    static uint8_t agc_gain_baseline = 0;
    static int8_t fft_gain_baseline = 0;
    esp_csi_gain_ctrl_get_rx_gain(rx_ctrl, &agc_gain, &fft_gain);
    if (s_count < 100) {
        esp_csi_gain_ctrl_record_rx_gain(agc_gain, fft_gain);
    } else if (s_count == 100) {
        esp_csi_gain_ctrl_get_rx_gain_baseline(&agc_gain_baseline, &fft_gain_baseline);
#if CONFIG_FORCE_GAIN
        esp_csi_gain_ctrl_set_rx_force_gain(agc_gain_baseline, fft_gain_baseline);
        ESP_LOGD(TAG, "fft_force %d, agc_force %d", fft_gain_baseline, agc_gain_baseline);
#endif
    }
    esp_csi_gain_ctrl_get_gain_compensation(&compensate_gain, agc_gain, fft_gain);
    /* (b) official per-packet ESP_LOGI of compensate_gain removed. */
#endif

    const uint32_t rx_id = *(uint32_t *)(info->payload + 15);

    /* Sequence gap accounting: the TX payload is a +1 counter. A smaller id
     * means the transmitter rebooted; resynchronise without counting a gap. */
    static bool s_have_last_id = false;
    static uint32_t s_last_id = 0;
    if (s_have_last_id && rx_id > s_last_id + 1) {
        s_seq_gaps += rx_id - s_last_id - 1;
    }
    s_last_id = rx_id;
    s_have_last_id = true;

    s_rx_total++;
    s_last_rssi = rx_ctrl->rssi;
    s_count++;

    eg_csi_item_t *item = &s_cb_item;
    memcpy(&item->rx_ctrl, rx_ctrl, sizeof(wifi_pkt_rx_ctrl_t));
    memcpy(item->mac, info->mac, 6);
    item->rx_id = rx_id;
    item->compensate_gain = compensate_gain;
    item->agc_gain = agc_gain;
    item->fft_gain = fft_gain;
    item->first_word_invalid = info->first_word_invalid;
    uint16_t n = info->len;
    if (n > EG_COPY_MAX) {
        n = EG_COPY_MAX;
    }
    item->len = n;
    memcpy(item->buf, info->buf, n);

    if (xQueueSend(s_csi_queue, item, 0) != pdTRUE) {
        s_queue_drops++;
    }
}

/* Append printf-style text to s_line, never overflowing. */
static size_t line_appendf(size_t pos, const char *fmt, ...) __attribute__((format(printf, 2, 3)));
static size_t line_appendf(size_t pos, const char *fmt, ...)
{
    if (pos >= sizeof(s_line)) {
        return pos;
    }
    va_list ap;
    va_start(ap, fmt);
    int w = vsnprintf(s_line + pos, sizeof(s_line) - pos, fmt, ap);
    va_end(ap);
    if (w < 0) {
        return pos;
    }
    pos += (size_t)w;
    return (pos < sizeof(s_line)) ? pos : sizeof(s_line) - 1;
}

static void print_csi_item(const eg_csi_item_t *it)
{
    const wifi_pkt_rx_ctrl_t *rx_ctrl = &it->rx_ctrl;
    size_t pos = 0;

#if EG_C5C6_FORMAT
    /* Official C5/C6 layout. The 'rx_state' / 'rx_format' column carries
     * cur_bb_format exactly as in the official example (kept: these targets
     * report the baseband format here and the official tools expect it). */
    pos = line_appendf(pos, "CSI_DATA,%" PRIu32 "," MACSTR ",%d,%d,%d,%d,%d,%d,%u,%d,%d",
                       it->rx_id, MAC2STR(it->mac), (int)rx_ctrl->rssi, (int)rx_ctrl->rate,
                       (int)rx_ctrl->noise_floor, (int)it->fft_gain, (int)it->agc_gain, (int)rx_ctrl->channel,
                       (unsigned)rx_ctrl->timestamp, (int)rx_ctrl->sig_len, (int)rx_ctrl->cur_bb_format);
#else
    /* Official classic layout; (d) rx_state column now prints rx_ctrl->rx_state. */
    pos = line_appendf(pos, "CSI_DATA,%" PRIu32 "," MACSTR ",%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%u,%d,%d,%d",
                       it->rx_id, MAC2STR(it->mac), (int)rx_ctrl->rssi, (int)rx_ctrl->rate, (int)rx_ctrl->sig_mode,
                       (int)rx_ctrl->mcs, (int)rx_ctrl->cwb, (int)rx_ctrl->smoothing, (int)rx_ctrl->not_sounding,
                       (int)rx_ctrl->aggregation, (int)rx_ctrl->stbc, (int)rx_ctrl->fec_coding, (int)rx_ctrl->sgi,
                       (int)rx_ctrl->noise_floor, (int)rx_ctrl->ampdu_cnt, (int)rx_ctrl->channel, (int)rx_ctrl->secondary_channel,
                       (unsigned)rx_ctrl->timestamp, (int)rx_ctrl->ant, (int)rx_ctrl->sig_len, (int)rx_ctrl->rx_state);
#endif

    const float g = it->compensate_gain;
#if (CONFIG_IDF_TARGET_ESP32C5 || CONFIG_IDF_TARGET_ESP32C61) && CSI_FORCE_LLTF
    /* Official 12-bit packed LLTF decoding for C5/C61 with force-LLTF. */
    const int n = (it->len >= 2) ? it->len : 2;
    int16_t csi = ((int16_t)(((((uint16_t)(uint8_t)it->buf[1]) << 8) | (uint8_t)it->buf[0]) << 4) >> 4);
    pos = line_appendf(pos, ",%d,%d,\"[%d", (n - 2) / 2, it->first_word_invalid, (int16_t)(g * csi));
    for (int i = 2; i < (n - 2); i += 2) {
        csi = ((int16_t)(((((uint16_t)(uint8_t)it->buf[i + 1]) << 8) | (uint8_t)it->buf[i]) << 4) >> 4);
        pos = line_appendf(pos, ",%d", (int16_t)(g * csi));
    }
#else
    if (it->len == 0) {
        return;  /* nothing to print; official code would read buf[0] */
    }
    pos = line_appendf(pos, ",%d,%d,\"[%d", it->len, it->first_word_invalid, (int16_t)(g * it->buf[0]));
    for (int i = 1; i < it->len; i++) {
        pos = line_appendf(pos, ",%d", (int16_t)(g * it->buf[i]));
    }
#endif
    pos = line_appendf(pos, "]\"\n");
    fputs(s_line, stdout);
}

static void print_header(void)
{
    ESP_LOGI(TAG, "================ CSI RECV ================");
#if EG_C5C6_FORMAT
    fputs("type,seq,mac,rssi,rate,noise_floor,fft_gain,agc_gain,channel,local_timestamp,sig_len,rx_format,len,first_word,data\n", stdout);
#else
    fputs("type,id,mac,rssi,rate,sig_mode,mcs,bandwidth,smoothing,not_sounding,aggregation,stbc,fec_coding,sgi,noise_floor,ampdu_cnt,channel,secondary_channel,local_timestamp,ant,sig_len,rx_state,len,first_word,data\n", stdout);
#endif
}

static void csi_print_task(void *arg)
{
    bool header_printed = false;
    uint32_t total_at_last = 0;
    int64_t last_stats_us = esp_timer_get_time();

    for (;;) {
        if (xQueueReceive(s_csi_queue, &s_print_item, pdMS_TO_TICKS(100)) == pdTRUE) {
            if (!header_printed) {
                print_header();
                header_printed = true;
            }
            print_csi_item(&s_print_item);
        }

        const int64_t now_us = esp_timer_get_time();
        const int64_t elapsed_us = now_us - last_stats_us;
        if (elapsed_us >= 1000000) {
            const uint32_t total = s_rx_total;
            const float rate_hz = (float)(total - total_at_last) * 1e6f / (float)elapsed_us;
            printf("EG_RX_STATS,%" PRIu32 ",%" PRIu32 ",%.1f,%" PRIu32 ",%" PRIu32 ",%" PRId32 "\n",
                   (uint32_t)(now_us / 1000), total, rate_hz, (uint32_t)s_queue_drops,
                   (uint32_t)s_seq_gaps, (int32_t)s_last_rssi);
            total_at_last = total;
            last_stats_us = now_us;
        }
    }
}

static void wifi_csi_init()
{
    ESP_ERROR_CHECK(esp_wifi_set_promiscuous(true));

    /**< default config */
#if CONFIG_IDF_TARGET_ESP32C5 || CONFIG_IDF_TARGET_ESP32C61
    wifi_csi_config_t csi_config = {
        .enable                   = true,
        .acquire_csi_legacy       = false,
        .acquire_csi_force_lltf   = CSI_FORCE_LLTF,
        .acquire_csi_ht20         = true,
        .acquire_csi_ht40         = true,
        .acquire_csi_vht          = false,
        .acquire_csi_su           = false,
        .acquire_csi_mu           = false,
        .acquire_csi_dcm          = false,
        .acquire_csi_beamformed   = false,
        .acquire_csi_he_stbc_mode = 2,
        .val_scale_cfg            = 0,
        .dump_ack_en              = false,
        .reserved                 = false
    };
#elif CONFIG_IDF_TARGET_ESP32C6
    wifi_csi_config_t csi_config = {
        .enable                 = true,
        .acquire_csi_legacy     = false,
        .acquire_csi_ht20       = true,
        .acquire_csi_ht40       = true,
        .acquire_csi_su         = true,
        .acquire_csi_mu         = true,
        .acquire_csi_dcm        = true,
        .acquire_csi_beamformed = true,
        .acquire_csi_he_stbc    = 2,
        .val_scale_cfg          = false,
        .dump_ack_en            = false,
        .reserved               = false
    };
#else
    wifi_csi_config_t csi_config = {
        .lltf_en           = true,
        .htltf_en          = true,
        .stbc_htltf2_en    = true,
        .ltf_merge_en      = true,
        .channel_filter_en = true,
        .manu_scale        = false,
        .shift             = false,
    };
#endif
    ESP_ERROR_CHECK(esp_wifi_set_csi_config(&csi_config));
    ESP_ERROR_CHECK(esp_wifi_set_csi_rx_cb(wifi_csi_rx_cb, NULL));
    ESP_ERROR_CHECK(esp_wifi_set_csi(true));
}

static void print_banner(void)
{
    printf("\n");
    printf("==================================================\n");
    printf(" EchoGuard RX (CSI receiver)\n");
    printf(" target        : %s\n", CONFIG_IDF_TARGET);
    printf(" esp-idf       : %s\n", esp_get_idf_version());
    printf(" channel       : %d (HT40, secondary %s)\n", CONFIG_LESS_INTERFERENCE_CHANNEL,
           (EG_SECOND_CHAN == WIFI_SECOND_CHAN_BELOW) ? "below" : "above");
    printf(" expected tx   : " MACSTR "\n", MAC2STR(CONFIG_CSI_SEND_MAC));
    printf(" line format   : %s\n", EG_C5C6_FORMAT ? "c5c6 (15 fields)" : "classic (25 fields)");
#if CONFIG_ECHOGUARD_RX_LLTF_ONLY
    printf(" lltf only     : yes (first %d values = 64 subcarriers)\n", EG_COPY_MAX);
#else
    printf(" lltf only     : no (up to %d values)\n", EG_COPY_MAX);
#endif
    printf(" queue depth   : %d\n", CONFIG_ECHOGUARD_RX_QUEUE_LEN);
    printf(" stats line    : EG_RX_STATS,<uptime_ms>,<csi_rx_total>,<csi_rx_rate_hz>,<queue_drops>,<seq_gaps>,<last_rssi>\n");
    printf("==================================================\n");
}

void app_main()
{
    /**
     * @brief Initialize NVS
     */
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret);

    print_banner();

    s_csi_queue = xQueueCreate(CONFIG_ECHOGUARD_RX_QUEUE_LEN, sizeof(eg_csi_item_t));
    if (s_csi_queue == NULL) {
        ESP_LOGE(TAG, "failed to allocate CSI queue (%d x %u bytes)",
                 CONFIG_ECHOGUARD_RX_QUEUE_LEN, (unsigned)sizeof(eg_csi_item_t));
        abort();
    }
    /* Lower priority than the Wi-Fi task so printing never starves reception. */
    if (xTaskCreate(csi_print_task, "eg_csi_print", 4096, NULL, 4, NULL) != pdPASS) {
        ESP_LOGE(TAG, "failed to create printer task");
        abort();
    }

    /**
     * @brief Initialize Wi-Fi
     */
    wifi_init();

    /**
     * @brief Initialize ESP-NOW
     *        ESP-NOW protocol see: https://docs.espressif.com/projects/esp-idf/en/latest/esp32/api-reference/network/esp_now.html
     */

    esp_now_peer_info_t peer = {
        .channel   = CONFIG_LESS_INTERFERENCE_CHANNEL,
        .ifidx     = WIFI_IF_STA,
        .encrypt   = false,
        .peer_addr = {0xff, 0xff, 0xff, 0xff, 0xff, 0xff},
    };

    wifi_esp_now_init(peer);

    wifi_csi_init();
}
