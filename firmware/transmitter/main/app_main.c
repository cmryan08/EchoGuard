/*
 * SPDX-FileCopyrightText: 2025-2026 Espressif Systems (Shanghai) CO LTD
 *
 * SPDX-License-Identifier: Apache-2.0
 */
/* EchoGuard CSI transmitter

   Derived from espressif/esp-csi examples/get-started/csi_send (Public Domain /
   CC0 example code). EchoGuard changes, kept deliberately minimal:
     - channel and send rate come from Kconfig (ECHOGUARD_TX_CHANNEL,
       ECHOGUARD_TX_RATE_HZ) instead of hard-coded defines
     - HT40 secondary channel is chosen so that any channel 1..13 is valid
       (official: always "below", which only works for channels 5..13)
     - startup banner with target, channel, rate, MAC and ESP-IDF version
     - vTaskDelayUntil() pacing for a consistent send rate (official: usleep)
     - esp_now_send() results are counted instead of logged per packet, and a
       once-per-second line is printed:
         EG_TX_STATS,<uptime_ms>,<sent_ok>,<send_fail>,<rate_hz>

   Everything else (fixed MAC 1a:00:00:00:00:00, ESP-NOW broadcast, MCS0 LGI
   rate config, per-target Wi-Fi setup) is unchanged from the official example.

   Unless required by applicable law or agreed to in writing, this
   software is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR
   CONDITIONS OF ANY KIND, either express or implied.
*/
#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include <inttypes.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include "nvs_flash.h"

#include "esp_mac.h"
#include "esp_log.h"
#include "esp_wifi.h"
#include "esp_netif.h"
#include "esp_now.h"
#include "esp_timer.h"
#include "esp_system.h"
#include "esp_idf_version.h"

#define CONFIG_LESS_INTERFERENCE_CHANNEL   CONFIG_ECHOGUARD_TX_CHANNEL

/* HT40 needs the secondary 20 MHz channel inside 1..13: "below" for 5..13
 * (official default for channel 11), "above" for 1..4. */
#define EG_SECOND_CHAN ((CONFIG_LESS_INTERFERENCE_CHANNEL >= 5) ? WIFI_SECOND_CHAN_BELOW : WIFI_SECOND_CHAN_ABOVE)

#if CONFIG_IDF_TARGET_ESP32C5 || CONFIG_IDF_TARGET_ESP32C61 || (CONFIG_IDF_TARGET_ESP32C6 && ESP_IDF_VERSION >= ESP_IDF_VERSION_VAL(5, 4, 0))
#define CONFIG_WIFI_BAND_MODE   WIFI_BAND_MODE_2G_ONLY
#define CONFIG_WIFI_2G_BANDWIDTHS           WIFI_BW_HT40
#define CONFIG_WIFI_5G_BANDWIDTHS           WIFI_BW_HT40
#define CONFIG_WIFI_2G_PROTOCOL             WIFI_PROTOCOL_11N
#define CONFIG_WIFI_5G_PROTOCOL             WIFI_PROTOCOL_11N
#else
#define CONFIG_WIFI_BANDWIDTH           WIFI_BW_HT40
#endif

#define CONFIG_ESP_NOW_PHYMODE           WIFI_PHY_MODE_HT40
#define CONFIG_ESP_NOW_RATE             WIFI_PHY_RATE_MCS0_LGI
#define CONFIG_SEND_FREQUENCY               CONFIG_ECHOGUARD_TX_RATE_HZ

#if ESP_IDF_VERSION >= ESP_IDF_VERSION_VAL(6, 0, 0)
#define ESP_IF_WIFI_STA ESP_MAC_WIFI_STA
#endif

static const uint8_t CONFIG_CSI_SEND_MAC[] = {0x1a, 0x00, 0x00, 0x00, 0x00, 0x00};
static const char *TAG = "csi_send";

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
    ESP_ERROR_CHECK(esp_wifi_set_mac(WIFI_IF_STA, CONFIG_CSI_SEND_MAC));
}

static void wifi_esp_now_init(esp_now_peer_info_t peer)
{
    ESP_ERROR_CHECK(esp_now_init());
    ESP_ERROR_CHECK(esp_now_set_pmk((uint8_t *)"pmk1234567890123"));
    ESP_ERROR_CHECK(esp_now_add_peer(&peer));
    esp_now_rate_config_t rate_config = {
        .phymode = CONFIG_ESP_NOW_PHYMODE,
        .rate = CONFIG_ESP_NOW_RATE,
        .ersu = false,
        .dcm = false
    };
    ESP_ERROR_CHECK(esp_now_set_peer_rate_config(peer.peer_addr, &rate_config));
}

static void print_banner(TickType_t period_ticks)
{
    uint8_t mac[6] = {0};
    esp_wifi_get_mac(WIFI_IF_STA, mac);
    const float effective_hz = (float)configTICK_RATE_HZ / (float)period_ticks;

    printf("\n");
    printf("==================================================\n");
    printf(" EchoGuard TX (ESP-NOW CSI transmitter)\n");
    printf(" target      : %s\n", CONFIG_IDF_TARGET);
    printf(" esp-idf     : %s\n", esp_get_idf_version());
    printf(" channel     : %d (HT40, secondary %s)\n", CONFIG_LESS_INTERFERENCE_CHANNEL,
           (EG_SECOND_CHAN == WIFI_SECOND_CHAN_BELOW) ? "below" : "above");
    printf(" rate        : %d Hz requested, %.1f Hz effective (tick %d Hz)\n",
           CONFIG_SEND_FREQUENCY, effective_hz, (int)configTICK_RATE_HZ);
    printf(" phy rate    : MCS0 LGI\n");
    printf(" tx mac      : " MACSTR "\n", MAC2STR(mac));
    printf(" destination : ff:ff:ff:ff:ff:ff (ESP-NOW broadcast, no router)\n");
    printf(" stats line  : EG_TX_STATS,<uptime_ms>,<sent_ok>,<send_fail>,<rate_hz>\n");
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

    TickType_t period_ticks = pdMS_TO_TICKS(1000 / CONFIG_SEND_FREQUENCY);
    if (period_ticks == 0) {
        period_ticks = 1;
    }

    ESP_LOGI(TAG, "================ CSI SEND ================");
    ESP_LOGI(TAG, "wifi_channel: %d, send_frequency: %d, mac: " MACSTR,
             CONFIG_LESS_INTERFERENCE_CHANNEL, CONFIG_SEND_FREQUENCY, MAC2STR(CONFIG_CSI_SEND_MAC));
    print_banner(period_ticks);

    uint32_t sent_ok = 0;
    uint32_t send_fail = 0;
    uint32_t no_mem = 0;
    esp_err_t last_err = ESP_OK;
    uint32_t ok_at_last_stats = 0;
    int64_t last_stats_us = esp_timer_get_time();
    TickType_t last_wake = xTaskGetTickCount();

    for (uint32_t count = 0; ; ++count) {
        /* Payload is the 32-bit packet counter, exactly as in the official
         * example; the receiver reads it back as the CSI_DATA 'id' column. */
        esp_err_t err = esp_now_send(peer.peer_addr, (const uint8_t *)&count, sizeof(count));
        if (err == ESP_OK) {
            sent_ok++;
        } else {
            send_fail++;
            last_err = err;
            if (err == ESP_ERR_ESPNOW_NO_MEM) {
                /* TX buffers full: back off briefly instead of spamming logs. */
                no_mem++;
                vTaskDelay(1);
            }
        }

        const int64_t now_us = esp_timer_get_time();
        const int64_t elapsed_us = now_us - last_stats_us;
        if (elapsed_us >= 1000000) {
            const float rate_hz = (float)(sent_ok - ok_at_last_stats) * 1e6f / (float)elapsed_us;
            printf("EG_TX_STATS,%" PRIu32 ",%" PRIu32 ",%" PRIu32 ",%.1f\n",
                   (uint32_t)(now_us / 1000), sent_ok, send_fail, rate_hz);
            if (last_err != ESP_OK) {
                /* At most one warning per second, only when errors happened. */
                ESP_LOGW(TAG, "esp_now_send errors so far: %" PRIu32 " (no_mem %" PRIu32 "), last <%s>, free_heap %" PRIu32,
                         send_fail, no_mem, esp_err_to_name(last_err), esp_get_free_heap_size());
                last_err = ESP_OK;
            }
            ok_at_last_stats = sent_ok;
            last_stats_us = now_us;
        }

        vTaskDelayUntil(&last_wake, period_ticks);
    }
}
