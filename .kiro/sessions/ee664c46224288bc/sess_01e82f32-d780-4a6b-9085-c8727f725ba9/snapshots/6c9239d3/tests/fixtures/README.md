# Test fixtures

These files are **format fixtures, not real hardware captures**. They exist so
the parser, buffer, replay and CLI can be tested without an ESP32. Never use
them as evidence of sensing or detection performance.

## classic_esp32s3_sample.txt

Classic ESP32 / ESP32-S3 / ESP32-C3 25-field format.

| Line(s) | Content |
| --- | --- |
| 1 | comment marking the file as a format fixture |
| 2 | ESP log line with ANSI colour codes |
| 3 | official classic header line |
| 4 | the example line from the official esp-csi `examples/get-started/README.md` (`CSI_DATA,0,94:d9:b3:80:8c:81,...`, `len` 128), copied verbatim |
| 5-19 | constructed lines in the same format: MAC `1a:00:00:00:00:00`, ids 1..12 with id 6 missing (one sequence gap), values = the official example array plus a small deterministic sine perturbation (zeros kept), `first_word` alternating 1/0, one line with an `I (...) csi_recv:` log prefix, one line ending in CRLF, plus an `EG_RX_STATS` line and an unrelated Wi-Fi log line |
| 20-23 | malformed CSI lines: truncated data array, wrong field count, non-numeric `rssi`, `len` mismatch (130 vs 128 values) |
| 24 | a final valid line with id 16 (ids 13-15 were malformed, so 3 more gaps) |

Expected parse result: 13 valid CSI packets, 4 malformed, 4 sequence gaps.

## c5c6_sample.txt

ESP32-C5 / ESP32-C6 15-field format.

| Line(s) | Content |
| --- | --- |
| 1-3 | fixture comment, log line, official C5/C6 header |
| 4 | the C5/C6 example line from the official README (`CSI_DATA,7,1a:00:00:00:00:00,...`, `len` 256), copied verbatim |
| 5-10 | constructed lines, seq 8..13, perturbed copies of the example array |
| 11 | `EG_RX_STATS` line |
| 12 | truncated (malformed) line |

Expected parse result: 7 valid CSI packets, 1 malformed, 0 sequence gaps.
