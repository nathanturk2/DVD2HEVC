/* Read-only DVD physical graph inspector for DVD2HEVC. */

#include <inttypes.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>

#include <dvdread/dvd_reader.h>
#include <dvdread/ifo_read.h>
#include <dvdread/nav_read.h>


static int64_t dvd_time_ticks(const dvd_time_t *time) {
    int64_t result;
    int64_t frames;
    result  = ((int64_t)(time->hour >> 4)) * 10 * 60 * 60 * 90000;
    result += ((int64_t)(time->hour & 0x0f)) * 60 * 60 * 90000;
    result += ((int64_t)(time->minute >> 4)) * 10 * 60 * 90000;
    result += ((int64_t)(time->minute & 0x0f)) * 60 * 90000;
    result += ((int64_t)(time->second >> 4)) * 10 * 90000;
    result += ((int64_t)(time->second & 0x0f)) * 90000;
    frames = ((time->frame_u & 0x30) >> 4) * 10 + (time->frame_u & 0x0f);
    result += frames * ((time->frame_u & 0x80) ? 3000 : 3600);
    return result;
}


static void json_string(const char *value) {
    const unsigned char *p = (const unsigned char *)value;
    putchar('"');
    while (*p) {
        switch (*p) {
            case '"': fputs("\\\"", stdout); break;
            case '\\': fputs("\\\\", stdout); break;
            case '\b': fputs("\\b", stdout); break;
            case '\f': fputs("\\f", stdout); break;
            case '\n': fputs("\\n", stdout); break;
            case '\r': fputs("\\r", stdout); break;
            case '\t': fputs("\\t", stdout); break;
            default:
                if (*p < 0x20) printf("\\u%04x", *p);
                else putchar(*p);
        }
        ++p;
    }
    putchar('"');
}


static void print_pgc(const pgc_t *pgc, int number, uint8_t entry_id) {
    unsigned i;
    const pgc_command_tbl_t *commands = pgc->command_tbl;
    printf("{\"number\":%d,\"entry_id\":%u,\"program_count\":%u,\"cell_count\":%u,"
           "\"duration_ticks\":%" PRId64 ",\"still_time\":%u,",
           number, entry_id, pgc->nr_of_programs, pgc->nr_of_cells,
           dvd_time_ticks(&pgc->playback_time), pgc->still_time);
    printf("\"command_counts\":{\"pre\":%u,\"post\":%u,\"cell\":%u},",
           commands ? commands->nr_of_pre : 0,
           commands ? commands->nr_of_post : 0,
           commands ? commands->nr_of_cell : 0);
    printf("\"program_map\":[");
    for (i = 0; i < pgc->nr_of_programs; ++i) {
        if (i) putchar(',');
        printf("%u", pgc->program_map ? pgc->program_map[i] : 0);
    }
    printf("],\"cells\":[");
    for (i = 0; i < pgc->nr_of_cells; ++i) {
        const cell_playback_t *play = &pgc->cell_playback[i];
        const cell_position_t *position = &pgc->cell_position[i];
        if (i) putchar(',');
        printf("{\"number\":%u,\"vob_id\":%u,\"cell_id\":%u,"
               "\"first_sector\":%u,\"first_ilvu_end_sector\":%u,"
               "\"last_vobu_start_sector\":%u,\"last_sector\":%u,"
               "\"duration_ticks\":%" PRId64 ",\"still_time\":%u,"
               "\"block_type\":%u,\"block_mode\":%u,\"seamless_play\":%s,"
               "\"interleaved\":%s,\"seamless_angle\":%s,\"stc_discontinuity\":%s}",
               i + 1, position->vob_id_nr, position->cell_nr,
               play->first_sector, play->first_ilvu_end_sector,
               play->last_vobu_start_sector, play->last_sector,
               dvd_time_ticks(&play->playback_time), play->still_time,
               play->block_type, play->block_mode,
               play->seamless_play ? "true" : "false",
               play->interleaved ? "true" : "false",
               play->seamless_angle ? "true" : "false",
               play->stc_discontinuity ? "true" : "false");
    }
    printf("]}");
}


static void print_pgcit(const pgcit_t *table) {
    unsigned i;
    putchar('[');
    if (table) {
        for (i = 0; i < table->nr_of_pgci_srp; ++i) {
            if (i) putchar(',');
            print_pgc(table->pgci_srp[i].pgc, (int)i + 1, table->pgci_srp[i].entry_id);
        }
    }
    putchar(']');
}


static void print_menu_pgci(const pgci_ut_t *table) {
    unsigned i;
    putchar('[');
    if (table) {
        for (i = 0; i < table->nr_of_lus; ++i) {
            if (i) putchar(',');
            unsigned high = table->lu[i].lang_code >> 8;
            unsigned low = table->lu[i].lang_code & 0xff;
            printf("{\"language_code\":");
            if (high >= 0x20 && high <= 0x7e && low >= 0x20 && low <= 0x7e)
                printf("\"%c%c\"", high, low);
            else
                printf("null");
            printf(",\"language_code_value\":%u,\"exists_mask\":%u,\"pgcs\":",
                   table->lu[i].lang_code, table->lu[i].exists);
            print_pgcit(table->lu[i].pgcit);
            putchar('}');
        }
    }
    putchar(']');
}


static void print_cell_addresses(const c_adt_t *table) {
    unsigned i, count = 0;
    if (table && table->last_byte + 1 >= C_ADT_SIZE)
        count = (table->last_byte + 1 - C_ADT_SIZE) / CELL_ADDR_SIZE;
    putchar('[');
    for (i = 0; i < count; ++i) {
        const cell_adr_t *cell = &table->cell_adr_table[i];
        if (i) putchar(',');
        printf("{\"vob_id\":%u,\"cell_id\":%u,\"start_sector\":%u,\"last_sector\":%u}",
               cell->vob_id, cell->cell_id, cell->start_sector, cell->last_sector);
    }
    putchar(']');
}


static int read_nav_pci(dvd_file_t *file, uint32_t sector, pci_t *pci) {
    unsigned char block[DVD_VIDEO_LB_LEN];
    unsigned char *p = block;
    unsigned packet_length;
    if (!file || DVDReadBlocks(file, (int)sector, 1, block) != 1)
        return 0;
    if (p[0] || p[1] || p[2] != 1 || p[3] != 0xba)
        return 0;
    p += ((p[4] & 0x40) == 0) ? 12 : 14 + (p[13] & 0x07);
    if (p + 6 > block + sizeof(block)) return 0;
    if (p[3] == 0xbb) {
        packet_length = ((unsigned)p[4] << 8) | p[5];
        p += 6 + packet_length;
    }
    if (p + 7 > block + sizeof(block) || p[0] || p[1] || p[2] != 1 || p[3] != 0xbf)
        return 0;
    packet_length = ((unsigned)p[4] << 8) | p[5];
    if (p + 6 + packet_length > block + sizeof(block) || p[6] != 0x00)
        return 0;
    navRead_PCI(pci, p + 7);
    return 1;
}


static void print_vobu_map(const vobu_admap_t *map, dvd_file_t *file) {
    unsigned i, count = 0;
    if (map && map->last_byte + 1 >= VOBU_ADMAP_SIZE)
        count = (map->last_byte + 1 - VOBU_ADMAP_SIZE) / sizeof(uint32_t);
    printf("{\"count\":%u,\"vobus\":[", count);
    for (i = 0; i < count; ++i) {
        pci_t pci;
        int have_pci = read_nav_pci(file, map->vobu_start_sectors[i], &pci);
        if (i) putchar(',');
        printf("{\"sector\":%u", map->vobu_start_sectors[i]);
        if (have_pci) {
            printf(",\"start_ptm\":%u,\"end_ptm\":%u,\"sequence_end_ptm\":%u,\"category\":%u",
                   pci.pci_gi.vobu_s_ptm, pci.pci_gi.vobu_e_ptm,
                   pci.pci_gi.vobu_se_e_ptm, pci.pci_gi.vobu_cat);
        } else {
            printf(",\"error\":\"NAV PCI unavailable\"");
        }
        putchar('}');
    }
    printf("]}");
}


static void print_chapters(const vts_ptt_srpt_t *table) {
    unsigned i, j;
    putchar('[');
    if (table) {
        for (i = 0; i < table->nr_of_srpts; ++i) {
            if (i) putchar(',');
            printf("{\"vts_title_number\":%u,\"parts\":[", i + 1);
            for (j = 0; j < table->title[i].nr_of_ptts; ++j) {
                if (j) putchar(',');
                printf("{\"part\":%u,\"pgc\":%u,\"program\":%u}",
                       j + 1, table->title[i].ptt[j].pgcn, table->title[i].ptt[j].pgn);
            }
            printf("]}");
        }
    }
    putchar(']');
}


int main(int argc, char **argv) {
    dvd_reader_t *dvd;
    dvd_file_t *vmg_menu_vobs;
    ifo_handle_t *vmg;
    unsigned i, vts_count;
    if (argc != 2) {
        fprintf(stderr, "usage: dvdinspect <dvd-iso-or-video-ts-path>\n");
        return 2;
    }
    dvd = DVDOpen(argv[1]);
    if (!dvd) {
        fprintf(stderr, "dvdinspect: libdvdread could not open %s\n", argv[1]);
        return 3;
    }
    vmg = ifoOpen(dvd, 0);
    if (!vmg || !vmg->vmgi_mat || !vmg->tt_srpt) {
        fprintf(stderr, "dvdinspect: could not read VIDEO_TS.IFO\n");
        if (vmg) ifoClose(vmg);
        DVDClose(dvd);
        return 4;
    }
    vmg_menu_vobs = DVDOpenFile(dvd, 0, DVD_READ_MENU_VOBS);
    vts_count = vmg->vmgi_mat->vmg_nr_of_title_sets;
    printf("{\"schema\":\"dvdinspect-physical-graph-v0\",\"source\":");
    json_string(argv[1]);
    printf(",\"vts_count\":%u,\"global_titles\":[", vts_count);
    for (i = 0; i < vmg->tt_srpt->nr_of_srpts; ++i) {
        const title_info_t *title = &vmg->tt_srpt->title[i];
        if (i) putchar(',');
        printf("{\"title\":%u,\"vts\":%u,\"vts_title_number\":%u,"
               "\"chapter_count\":%u,\"angle_count\":%u,\"title_set_sector\":%u,"
               "\"has_cell_commands\":%s,\"has_prepost_commands\":%s,"
               "\"has_button_commands\":%s}",
               i + 1, title->title_set_nr, title->vts_ttn, title->nr_of_ptts,
               title->nr_of_angles, title->title_set_sector,
               title->pb_ty.jlc_exists_in_cell_cmd ? "true" : "false",
               title->pb_ty.jlc_exists_in_prepost_cmd ? "true" : "false",
               title->pb_ty.jlc_exists_in_button_cmd ? "true" : "false");
    }
    printf("],\"vmg_menu_pgci\":");
    print_menu_pgci(vmg->pgci_ut);
    printf(",\"vmg_menu_vobs_sector\":%u", vmg->vmgi_mat->vmgm_vobs);
    printf(",\"vmg_menu_cell_addresses\":");
    print_cell_addresses(vmg->menu_c_adt);
    printf(",\"vmg_menu_vobu_map\":");
    print_vobu_map(vmg->menu_vobu_admap, vmg_menu_vobs);
    printf(",\"title_sets\":[");
    for (i = 1; i <= vts_count; ++i) {
        ifo_handle_t *vts = ifoOpen(dvd, (int)i);
        dvd_file_t *title_vobs = DVDOpenFile(dvd, (int)i, DVD_READ_TITLE_VOBS);
        dvd_file_t *menu_vobs = DVDOpenFile(dvd, (int)i, DVD_READ_MENU_VOBS);
        if (i > 1) putchar(',');
        if (!vts || !vts->vtsi_mat) {
            printf("{\"vts\":%u,\"error\":\"could not open VTS IFO\"}", i);
            if (vts) ifoClose(vts);
            if (title_vobs) DVDCloseFile(title_vobs);
            if (menu_vobs) DVDCloseFile(menu_vobs);
            continue;
        }
        printf("{\"vts\":%u,\"last_sector\":%u,\"title_vobs_sector\":%u,"
               "\"menu_vobs_sector\":%u,\"audio_stream_count\":%u,"
               "\"subpicture_stream_count\":%u,\"video_mpeg_version\":%u,"
               "\"video_format\":%u,\"display_aspect_ratio\":%u,",
               i, vts->vtsi_mat->vts_last_sector, vts->vtsi_mat->vtstt_vobs,
               vts->vtsi_mat->vtsm_vobs, vts->vtsi_mat->nr_of_vts_audio_streams,
               vts->vtsi_mat->nr_of_vts_subp_streams, vts->vtsi_mat->vts_video_attr.mpeg_version,
               vts->vtsi_mat->vts_video_attr.video_format,
               vts->vtsi_mat->vts_video_attr.display_aspect_ratio);
        printf("\"chapters\":"); print_chapters(vts->vts_ptt_srpt);
        printf(",\"title_pgcs\":"); print_pgcit(vts->vts_pgcit);
        printf(",\"menu_pgci\":"); print_menu_pgci(vts->pgci_ut);
        printf(",\"title_cell_addresses\":"); print_cell_addresses(vts->vts_c_adt);
        printf(",\"menu_cell_addresses\":"); print_cell_addresses(vts->menu_c_adt);
        printf(",\"title_vobu_map\":"); print_vobu_map(vts->vts_vobu_admap, title_vobs);
        printf(",\"menu_vobu_map\":"); print_vobu_map(vts->menu_vobu_admap, menu_vobs);
        putchar('}');
        if (title_vobs) DVDCloseFile(title_vobs);
        if (menu_vobs) DVDCloseFile(menu_vobs);
        ifoClose(vts);
    }
    printf("]}\n");
    if (vmg_menu_vobs) DVDCloseFile(vmg_menu_vobs);
    ifoClose(vmg);
    DVDClose(dvd);
    return 0;
}
