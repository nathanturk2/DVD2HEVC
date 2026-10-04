/* Headless libdvdnav menu-action trace for DVD2HEVC feedback gates. */

#include <inttypes.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <dvdnav/dvdnav.h>


int main(int argc, char **argv) {
    dvdnav_t *nav = NULL;
    uint8_t block[DVD_VIDEO_LB_LEN];
    int activations_requested;
    int requested_buttons[20] = {0};
    int requested_button_count = 0;
    int activations_done = 0;
    int activation_pending = 0;
    int finished = 0;
    int calls = 0;

    if (argc != 4) {
        fprintf(stderr, "usage: dvdnavtrace <dvd-iso-or-video-ts> <activations> <button>\n");
        return 2;
    }
    activations_requested = atoi(argv[2]);
    {
        char *token = strtok(argv[3], ",");
        while (token && requested_button_count < 20) {
            requested_buttons[requested_button_count++] = atoi(token);
            token = strtok(NULL, ",");
        }
    }
    if (activations_requested < 0 || activations_requested > 20) {
        fprintf(stderr, "dvdnavtrace: activations must be between 0 and 20\n");
        return 2;
    }
    if (requested_button_count != 1 && requested_button_count != activations_requested) {
        fprintf(stderr, "dvdnavtrace: supply one button or one per activation\n");
        return 2;
    }
    {
        int i;
        for (i = 0; i < requested_button_count; ++i) {
            if (requested_buttons[i] < 0 || requested_buttons[i] > 36) {
                fprintf(stderr, "dvdnavtrace: buttons must be zero (current) or 1-36\n");
                return 2;
            }
        }
    }
    if (dvdnav_open(&nav, argv[1]) != DVDNAV_STATUS_OK) {
        fprintf(stderr, "dvdnavtrace: dvdnav_open failed\n");
        return 3;
    }
    dvdnav_set_readahead_flag(nav, 0);
    dvdnav_menu_language_select(nav, "en");
    dvdnav_audio_language_select(nav, "en");
    dvdnav_spu_language_select(nav, "en");
    printf("TRACE OPEN\n");

    while (!finished && calls++ < 500000) {
        int event = 0;
        int length = 0;
        if (dvdnav_get_next_block(nav, block, &event, &length) != DVDNAV_STATUS_OK) {
            fprintf(stderr, "dvdnavtrace: read failed: %s\n", dvdnav_err_to_string(nav));
            dvdnav_close(nav);
            return 4;
        }
        switch (event) {
            case DVDNAV_BLOCK_OK:
            case DVDNAV_NOP:
                break;
            case DVDNAV_NAV_PACKET: {
                pci_t *pci = dvdnav_get_current_nav_pci(nav);
                unsigned buttons = pci ? pci->hli.hl_gi.btn_ns : 0;
                if (buttons > 0) {
                    int current = 0;
                    dvdnav_get_current_highlight(nav, &current);
                    printf("TRACE NAV buttons=%u current=%d\n", buttons, current);
                    if (!activation_pending && activations_done < activations_requested) {
                        int requested = requested_buttons[
                            requested_button_count == 1 ? 0 : activations_done
                        ];
                        int button = requested > 0 ? requested : current;
                        if (button <= 0)
                            button = 1;
                        if ((unsigned)button > buttons) {
                            fprintf(stderr, "dvdnavtrace: requested button %d, menu has %u\n",
                                    button, buttons);
                            dvdnav_close(nav);
                            return 6;
                        }
                        int result = dvdnav_button_select_and_activate(nav, pci, button);
                        printf("TRACE ACTIVATE index=%d button=%d status=%d\n",
                               activations_done + 1, button, result);
                        ++activations_done;
                        activation_pending = 1;
                    }
                }
                break;
            }
            case DVDNAV_HIGHLIGHT: {
                dvdnav_highlight_event_t *value = (dvdnav_highlight_event_t *)block;
                printf("TRACE HIGHLIGHT display=%d button=%d\n", value->display, value->buttonN);
                break;
            }
            case DVDNAV_VTS_CHANGE: {
                dvdnav_vts_change_event_t *value = (dvdnav_vts_change_event_t *)block;
                printf("TRACE VTS_CHANGE old_vts=%d old_domain=%d new_vts=%d new_domain=%d\n",
                       value->old_vtsN, value->old_domain, value->new_vtsN, value->new_domain);
                break;
            }
            case DVDNAV_CELL_CHANGE: {
                dvdnav_cell_change_event_t *value = (dvdnav_cell_change_event_t *)block;
                printf("TRACE CELL_CHANGE cell=%d program=%d cell_length=%" PRIu64
                       " program_length=%" PRIu64 " pgc_length=%" PRIu64 "\n",
                       value->cellN, value->pgN, value->cell_length,
                       value->pg_length, value->pgc_length);
                if (activation_pending) {
                    activation_pending = 0;
                    if (activations_done >= activations_requested)
                        finished = 1;
                }
                break;
            }
            case DVDNAV_STILL_FRAME: {
                dvdnav_still_event_t *value = (dvdnav_still_event_t *)block;
                printf("TRACE STILL length=%u\n", value->length);
                if (!activation_pending)
                    dvdnav_still_skip(nav);
                break;
            }
            case DVDNAV_WAIT:
                printf("TRACE WAIT\n");
                dvdnav_wait_skip(nav);
                break;
            case DVDNAV_SPU_CLUT_CHANGE:
                printf("TRACE SPU_CLUT_CHANGE\n");
                break;
            case DVDNAV_SPU_STREAM_CHANGE: {
                dvdnav_spu_stream_change_event_t *value = (dvdnav_spu_stream_change_event_t *)block;
                printf("TRACE SPU_STREAM wide=%d letterbox=%d pan_scan=%d\n",
                       value->physical_wide, value->physical_letterbox, value->physical_pan_scan);
                break;
            }
            case DVDNAV_AUDIO_STREAM_CHANGE: {
                dvdnav_audio_stream_change_event_t *value = (dvdnav_audio_stream_change_event_t *)block;
                printf("TRACE AUDIO_STREAM physical=%d logical=%d\n", value->physical, value->logical);
                break;
            }
            case DVDNAV_HOP_CHANNEL:
                printf("TRACE HOP_CHANNEL\n");
                break;
            case DVDNAV_STOP:
                printf("TRACE STOP\n");
                finished = 1;
                break;
            default:
                printf("TRACE EVENT code=%d length=%d\n", event, length);
                break;
        }
        fflush(stdout);
    }
    printf("TRACE RESULT activations=%d requested=%d completed=%s calls=%d\n",
           activations_done, activations_requested,
           activations_done == activations_requested ? "true" : "false", calls);
    dvdnav_close(nav);
    return activations_done == activations_requested ? 0 : 5;
}
