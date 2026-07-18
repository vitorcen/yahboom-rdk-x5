/*
 * stereo_capture — continuous dual SC132GS (GS130WI) capture daemon.
 *
 * Based on RDK sample get_isp_data (vin->isp HBN vflow per sensor). Opens BOTH
 * eyes (-s <idx> twice, e.g. -s 4 -s 5 = right 0x30 + left 0x32), then each eye
 * gets a thread that pulls AE-processed NV12 frames from its ISP chn0 and
 * publishes the latest one to /dev/shm/stereo_cam<i>.nv12 (write tmp + rename,
 * so readers always see a complete frame).
 *
 * File layout: 32-byte header + NV12 payload.
 *   header: magic "STER"(4) | u32 width | u32 height | u32 stride
 *         | u32 frame_id | u64 timestamp_ns | u32 reserved
 *
 * Build (on board):
 *   gcc -O2 -o stereo_capture stereo_capture.c \
 *       -I/usr/hobot/include -I/app/multimedia_samples/include \
 *       -I/app/multimedia_samples/utils -I/app/multimedia_samples/vp_sensors \
 *       /app/multimedia_samples/utils/common_utils.o \
 *       $(find /app/multimedia_samples/vp_sensors -name '*.o') \
 *       -L/usr/hobot/lib -lcam -lvpf -lhbmem -lgdcbin -lcjson -lpthread -lalog -ldl
 *
 * The vp_sensors .o pool must already contain the left-eye variant
 * (linear_1088x1280_raw10_30fps_1lane_left.c, addr 0x32) — see
 * .memory/rdk-x5-stereo-camera.md.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <unistd.h>
#include <fcntl.h>
#include <signal.h>
#include <pthread.h>
#include <getopt.h>
#include <sys/mman.h>
#include <time.h>

#include "common_utils.h"
#include "hbn_isp_api.h"

#define MAX_SENSORS 2

/* Paired output: /dev/shm/stereo_pair.shm, seqlock-protected mmap.
 *
 * Per-eye files + consumer-side ts pairing collapsed under load: each eye
 * drops ~13% of its 60fps writes independently, so the "a near pair always
 * exists" assumption dies (measured dt p50=12ms > 8.5ms gate). This daemon
 * sees BOTH streams, so it pairs at the source: each eye stages its newest
 * frame; whichever eye lands second checks the other's ts and publishes the
 * pair in one seqlocked write. Readers mmap once, copy, verify seq.
 *
 * Layout: 64B header | eye0 NV12 | eye1 NV12
 *   header: "STPR"(4) | u32 seq (odd = write in progress) | u32 w | u32 h
 *         | u64 ts0_ns | u64 ts1_ns | u32 fid0 | u32 fid1 | pad to 64
 */
#define PAIR_PATH   "/dev/shm/stereo_pair.shm"
#define FRAME_BYTES (1088 * 1280 * 3 / 2)
#define PAIR_HDR    64
#define PAIR_MAX_DT_NS 8500000LL

static volatile sig_atomic_t g_run = 1;
static int g_tap_vin = 0;       /* STEREO_TAP=vin -> dump pre-ISP RAW instead */
static int g_fps = 0;           /* -f N: override LPWM trigger rate (sensor is
                                 * external-trigger: vts=0x3fff, frame rate ==
                                 * LPWM period; stock configs = 33333us = 30fps) */
static void on_sig(int s) { (void)s; g_run = 0; }

typedef struct {
    pipe_contex_t *pipe;
    int idx;                    /* 0/1 -> /dev/shm/stereo_cam<idx>.nv12 */
} eye_arg_t;

/* --- node creation: same shape as get_isp_data sample --- */

static int create_camera_node(pipe_contex_t *p) {
    /* mipi phy fixed up beforehand by vp_sensor_fixed_mipi_host() in main */
    int32_t ret = hbn_camera_create(p->sensor_config->camera_config, &p->cam_fd);
    ERR_CON_EQ(ret, 0);
    return 0;
}

static int create_vin_node(pipe_contex_t *p) {
    vp_sensor_config_t *sc = p->sensor_config;
    /* hw_id MUST be the mipi rx this eye sits on (fixed up by
     * vp_sensor_fixed_mipi_host); opening both eyes on hw_id 0 makes the
     * second attach fail. Flyby (vin->isp online) needs no vin ochn buffers. */
    uint32_t hw_id = sc->vin_node_attr->cim_attr.mipi_rx;
    uint32_t chn_id = 0;
    int32_t ret;
    vin_attr_ex_t vin_attr_ex;

    if (p->csi_config.mclk_is_not_configed) {
        vin_attr_ex.vin_attr_ex_mask = 0x00;    /* external oscillator */
    } else {
        vin_attr_ex.vin_attr_ex_mask = 0x80;    /* bit7 = mclk */
        vin_attr_ex.mclk_ex_attr.mclk_freq = 24000000;
    }

    ret = hbn_vnode_open(HB_VIN, hw_id, AUTO_ALLOC_ID, &p->vin_node_handle);
    ERR_CON_EQ(ret, 0);
    ret = hbn_vnode_set_attr(p->vin_node_handle, sc->vin_node_attr);
    ERR_CON_EQ(ret, 0);
    ret = hbn_vnode_set_ichn_attr(p->vin_node_handle, chn_id, sc->vin_ichn_attr);
    ERR_CON_EQ(ret, 0);
    ret = hbn_vnode_set_ochn_attr(p->vin_node_handle, chn_id, sc->vin_ochn_attr);
    ERR_CON_EQ(ret, 0);

    for (uint8_t i = 0; vin_attr_ex.vin_attr_ex_mask && i < VIN_ATTR_EX_INVALID; i++) {
        if ((vin_attr_ex.vin_attr_ex_mask & (1u << i)) == 0)
            continue;
        vin_attr_ex.ex_attr_type = i;
        ret = hbn_vnode_set_attr_ex(p->vin_node_handle, &vin_attr_ex);
        ERR_CON_EQ(ret, 0);
    }

    if (g_tap_vin) {            /* RAW tap needs DDR buffers on vin ochn */
        hbn_buf_alloc_attr_t alloc_attr = {0};
        alloc_attr.buffers_num = 3;
        alloc_attr.is_contig = 1;
        alloc_attr.flags = HB_MEM_USAGE_CPU_READ_OFTEN | HB_MEM_USAGE_CPU_WRITE_OFTEN
                         | HB_MEM_USAGE_CACHED;
        ret = hbn_vnode_set_ochn_buf_attr(p->vin_node_handle, chn_id, &alloc_attr);
        ERR_CON_EQ(ret, 0);
    }
    return 0;
}

static int create_isp_node(pipe_contex_t *p) {
    vp_sensor_config_t *sc = p->sensor_config;
    uint32_t chn_id = 0;
    int32_t ret;
    hbn_buf_alloc_attr_t alloc_attr = {0};

    ret = hbn_vnode_open(HB_ISP, 0, AUTO_ALLOC_ID, &p->isp_node_handle);
    ERR_CON_EQ(ret, 0);
    ret = hbn_vnode_set_attr(p->isp_node_handle, sc->isp_attr);
    ERR_CON_EQ(ret, 0);
    ret = hbn_vnode_set_ochn_attr(p->isp_node_handle, chn_id, sc->isp_ochn_attr);
    ERR_CON_EQ(ret, 0);
    ret = hbn_vnode_set_ichn_attr(p->isp_node_handle, chn_id, sc->isp_ichn_attr);
    ERR_CON_EQ(ret, 0);
    alloc_attr.buffers_num = 3;
    alloc_attr.is_contig = 1;
    alloc_attr.flags = HB_MEM_USAGE_CPU_READ_OFTEN | HB_MEM_USAGE_CPU_WRITE_OFTEN
                     | HB_MEM_USAGE_CACHED;
    ret = hbn_vnode_set_ochn_buf_attr(p->isp_node_handle, chn_id, &alloc_attr);
    ERR_CON_EQ(ret, 0);
    return 0;
}

static int create_and_run_vflow(pipe_contex_t *p) {
    int32_t ret;
    ret = create_camera_node(p);            ERR_CON_EQ(ret, 0);
    ret = create_vin_node(p);               ERR_CON_EQ(ret, 0);
    ret = create_isp_node(p);               ERR_CON_EQ(ret, 0);
    ret = hbn_vflow_create(&p->vflow_fd);   ERR_CON_EQ(ret, 0);
    ret = hbn_vflow_add_vnode(p->vflow_fd, p->vin_node_handle);  ERR_CON_EQ(ret, 0);
    ret = hbn_vflow_add_vnode(p->vflow_fd, p->isp_node_handle);  ERR_CON_EQ(ret, 0);
    ret = hbn_vflow_bind_vnode(p->vflow_fd, p->vin_node_handle, 1,
                               p->isp_node_handle, 0);           ERR_CON_EQ(ret, 0);
    ret = hbn_camera_attach_to_vin(p->cam_fd, p->vin_node_handle); ERR_CON_EQ(ret, 0);
    ret = hbn_vflow_start(p->vflow_fd);     ERR_CON_EQ(ret, 0);
    return 0;
}

/* AWB auto never converges on this module (no OTP, "can not calculate right
 * color temperature" forever) and keeps wandering, so downstream color
 * correction chases a moving target. Freeze WB: prefer the tuning-table gains
 * for ISP_WB_TEMPER (default 4500K; table is empty on GS130WI but harmless),
 * else ISP_WB_GAINS="r,b", else lock whatever auto has settled on by now. */
static void freeze_isp_wb(pipe_contex_t *p, int idx) {
    uint32_t temper = 4500;
    const char *e = getenv("ISP_WB_TEMPER");
    if (e) temper = (uint32_t)atoi(e);
    if (!temper) return;                    /* ISP_WB_TEMPER=0 keeps auto */

    hbn_isp_awb_attr_t attr;
    memset(&attr, 0, sizeof(attr));
    if (hbn_isp_get_awb_attr(p->isp_node_handle, &attr) != 0) return;

    hbn_isp_awb_gain_t g;
    memset(&g, 0, sizeof(g));
    hbn_isp_get_awb_gain_by_temper(p->isp_node_handle, temper, &g);
    if (g.rgain <= 0.01f || g.bgain <= 0.01f) {
        const char *gv = getenv("ISP_WB_GAINS");
        float r = 0.0f, b = 0.0f;
        if (gv && sscanf(gv, "%f,%f", &r, &b) == 2 && r > 0.01f && b > 0.01f) {
            g.rgain = r; g.grgain = 1.0f; g.gbgain = 1.0f; g.bgain = b;
        } else if (attr.auto_attr.gain.rgain > 0.01f) {
            g = attr.auto_attr.gain;        /* freeze current auto estimate */
        } else {
            return;
        }
    }

    attr.mode = HBN_ISP_MODE_MANUAL;
    attr.manual_attr.gain = g;
    attr.manual_attr.temper = temper;
    int32_t ret = hbn_isp_set_awb_attr(p->isp_node_handle, &attr);
    printf("eye%d: WB frozen r=%.3f gr=%.3f gb=%.3f b=%.3f (ret=%d)\n",
           idx, g.rgain, g.grgain, g.gbgain, g.bgain, ret);
}

/* --- paired shm publishing --- */

typedef struct {
    uint8_t data[FRAME_BYTES];
    uint64_t ts;                /* sensor timestamp, ns */
    uint32_t fid;
    int valid;
} stage_t;

static stage_t g_stage[MAX_SENSORS];
static pthread_mutex_t g_pair_mtx = PTHREAD_MUTEX_INITIALIZER;
static uint8_t *g_pair_map = NULL;
static uint64_t g_last_pair_ns = 0;
static uint64_t g_pair_min_ns = 33333333;   /* PAIR_HZ=30 default */
static uint32_t g_pairs = 0, g_frames = 0;  /* stats */

static int pair_map_init(void) {
    int fd = open(PAIR_PATH, O_RDWR | O_CREAT, 0644);
    if (fd < 0) return -1;
    if (ftruncate(fd, PAIR_HDR + 2 * (off_t)FRAME_BYTES) != 0) { close(fd); return -1; }
    g_pair_map = mmap(NULL, PAIR_HDR + 2 * (size_t)FRAME_BYTES,
                      PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    close(fd);
    if (g_pair_map == MAP_FAILED) { g_pair_map = NULL; return -1; }
    memcpy(g_pair_map, "STPR", 4);
    uint32_t w = 1088, h = 1280;
    memcpy(g_pair_map + 8, &w, 4);
    memcpy(g_pair_map + 12, &h, 4);
    return 0;
}

/* caller holds g_pair_mtx */
static void publish_pair(void) {
    uint32_t *seq = (uint32_t *)(g_pair_map + 4);
    __atomic_store_n(seq, *seq + 1, __ATOMIC_RELEASE);      /* odd: writing */
    memcpy(g_pair_map + 16, &g_stage[0].ts, 8);
    memcpy(g_pair_map + 24, &g_stage[1].ts, 8);
    memcpy(g_pair_map + 32, &g_stage[0].fid, 4);
    memcpy(g_pair_map + 36, &g_stage[1].fid, 4);
    memcpy(g_pair_map + PAIR_HDR, g_stage[0].data, FRAME_BYTES);
    memcpy(g_pair_map + PAIR_HDR + FRAME_BYTES, g_stage[1].data, FRAME_BYTES);
    __atomic_store_n(seq, *seq + 1, __ATOMIC_RELEASE);      /* even: done */
    g_pairs++;
}

static void stage_frame(int idx, hbn_vnode_image_t *img) {
    if ((size_t)(img->buffer.size[0] + img->buffer.size[1]) > FRAME_BYTES)
        return;                                             /* RAW tap etc. */
    pthread_mutex_lock(&g_pair_mtx);
    stage_t *s = &g_stage[idx], *o = &g_stage[1 - idx];
    memcpy(s->data, img->buffer.virt_addr[0], img->buffer.size[0]);
    memcpy(s->data + img->buffer.size[0], img->buffer.virt_addr[1], img->buffer.size[1]);
    s->ts = (uint64_t)img->info.timestamps;
    s->fid = img->info.frame_id;
    s->valid = 1;
    g_frames++;
    int64_t dt = (int64_t)(s->ts - o->ts);
    if (o->valid && llabs(dt) <= PAIR_MAX_DT_NS &&
        s->ts - g_last_pair_ns >= g_pair_min_ns) {
        publish_pair();
        g_last_pair_ns = s->ts;
    }
    pthread_mutex_unlock(&g_pair_mtx);
}

/* legacy single-eye dump, kept for the STEREO_TAP=vin RAW debug path */
static int write_shm_frame(int idx, hbn_vnode_image_t *img) {
    char tmp[64], dst[64];
    snprintf(tmp, sizeof(tmp), "/dev/shm/.stereo_cam%d.tmp", idx);
    snprintf(dst, sizeof(dst), "/dev/shm/stereo_cam%d.nv12", idx);

    int fd = open(tmp, O_WRONLY | O_CREAT | O_TRUNC, 0644);
    if (fd < 0) return -1;

    uint8_t hdr[32] = {0};
    memcpy(hdr, "STER", 4);
    uint32_t w = img->buffer.width, h = img->buffer.height, st = img->buffer.stride;
    uint32_t fid = img->info.frame_id;
    uint64_t ts = (uint64_t)img->info.timestamps;
    memcpy(hdr + 4,  &w,  4);
    memcpy(hdr + 8,  &h,  4);
    memcpy(hdr + 12, &st, 4);
    memcpy(hdr + 16, &fid, 4);
    memcpy(hdr + 20, &ts, 8);

    ssize_t ok = write(fd, hdr, sizeof(hdr)) == (ssize_t)sizeof(hdr);
    ok = ok && write(fd, img->buffer.virt_addr[0], img->buffer.size[0]) == (ssize_t)img->buffer.size[0];
    ok = ok && write(fd, img->buffer.virt_addr[1], img->buffer.size[1]) == (ssize_t)img->buffer.size[1];
    close(fd);
    if (!ok) { unlink(tmp); return -1; }
    return rename(tmp, dst);
}

static void *eye_thread(void *argp) {
    eye_arg_t *a = (eye_arg_t *)argp;
    hbn_vnode_handle_t isp = g_tap_vin ? a->pipe->vin_node_handle
                                       : a->pipe->isp_node_handle;
    uint32_t n = 0;
    while (g_run) {
        hbn_vnode_image_t img;
        int ret = hbn_vnode_getframe(isp, 0, 2000, &img);
        if (ret != 0) {
            fprintf(stderr, "eye%d getframe failed(%d)\n", a->idx, ret);
            usleep(100 * 1000);
            continue;
        }
        (void)n;
        if (g_tap_vin)
            write_shm_frame(a->idx, &img);
        else
            stage_frame(a->idx, &img);
        hbn_vnode_releaseframe(isp, 0, &img);
    }
    return NULL;
}

int main(int argc, char **argv) {
    pipe_contex_t pipes[MAX_SENSORS];
    int idxs[MAX_SENSORS], count = 0;
    int c;

    memset(pipes, 0, sizeof(pipes));
    while ((c = getopt(argc, argv, "s:f:h")) != -1) {
        if (c == 's' && count < MAX_SENSORS) idxs[count++] = atoi(optarg);
        else if (c == 'f') g_fps = atoi(optarg);
        else { printf("usage: %s -s <idx> [-s <idx>] [-f fps]\n", argv[0]); vp_show_sensors_list(); return 1; }
    }
    if (count < 1) { printf("need -s <sensor_index> (one or two)\n"); vp_show_sensors_list(); return 1; }

    signal(SIGINT, on_sig);
    signal(SIGTERM, on_sig);
    g_tap_vin = (getenv("STEREO_TAP") && !strcmp(getenv("STEREO_TAP"), "vin"));
    if (g_tap_vin) printf("tap=vin (RAW10 pre-ISP)\n");
    if (getenv("PAIR_HZ")) {
        int hz = atoi(getenv("PAIR_HZ"));
        if (hz > 0) g_pair_min_ns = 1000000000ULL / hz;
    }
    if (!g_tap_vin && pair_map_init() != 0) {
        printf("pair map init failed\n");
        return 1;
    }
    hb_mem_module_open();

    for (int i = 0; i < count; i++) {
        if (idxs[i] >= vp_get_sensors_list_number() || idxs[i] < 0) {
            printf("bad sensor index %d\n", idxs[i]); return 1;
        }
        pipes[i].sensor_config = vp_sensor_config_list[idxs[i]];
        printf("eye%d: index:%d name:%s\n", i, idxs[i], pipes[i].sensor_config->sensor_name);
        if (g_fps > 0) {   /* retune trigger rate: LPWM period + every fps field
                            * the SDK might derive the period from */
            int period = 1000000 / g_fps;
            lpwm_attr_t *lp = &pipes[i].sensor_config->vin_node_attr->lpwm_attr;
            for (int ch = 0; ch < 4; ch++) lp->lpwm_chn_attr[ch].period = period;
            pipes[i].sensor_config->camera_config->fps = g_fps;
            pipes[i].sensor_config->camera_config->mipi_cfg->rx_attr.fps = g_fps;
            printf("eye%d: lpwm period=%dus + cfg fps=%d\n", i, period, g_fps);
        }
        if (vp_sensor_fixed_mipi_host(pipes[i].sensor_config, &pipes[i].csi_config) != 0) {
            printf("eye%d: sensor not found on any CSI (check connection)\n", i); return 1;
        }
        if (create_and_run_vflow(&pipes[i]) != 0) {
            printf("vflow for eye%d failed\n", i); return 1;
        }
    }

    pthread_t th[MAX_SENSORS];
    eye_arg_t args[MAX_SENSORS];
    for (int i = 0; i < count; i++) {
        args[i].pipe = &pipes[i];
        args[i].idx = i;
        pthread_create(&th[i], NULL, eye_thread, &args[i]);
    }
    printf("stereo_capture running: %s\n", g_tap_vin ? "/dev/shm/stereo_cam{0,1}.nv12 (RAW)" : PAIR_PATH);
    if (!g_tap_vin) {
        sleep(3);               /* frames flowing -> 3A attached, auto settled */
        for (int i = 0; i < count; i++) freeze_isp_wb(&pipes[i], i);
        while (g_run) {
            sleep(10);
            pthread_mutex_lock(&g_pair_mtx);
            printf("pairs=%u frames=%u in 10s\n", g_pairs, g_frames);
            fflush(stdout);
            g_pairs = g_frames = 0;
            pthread_mutex_unlock(&g_pair_mtx);
        }
    }
    for (int i = 0; i < count; i++) pthread_join(th[i], NULL);

    for (int i = 0; i < count; i++) {
        hbn_vflow_stop(pipes[i].vflow_fd);
        hbn_vflow_destroy(pipes[i].vflow_fd);
        hbn_camera_destroy(pipes[i].cam_fd);
    }
    hb_mem_module_close();
    return 0;
}
