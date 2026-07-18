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

#include "common_utils.h"

#define MAX_SENSORS 2
#define WRITE_EVERY 1           /* write every frame: readers pair by ts */

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

/* --- shm publishing --- */

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
        if (n++ % WRITE_EVERY == 0)
            write_shm_frame(a->idx, &img);
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
    printf("stereo_capture running: /dev/shm/stereo_cam{0,1}.nv12 (~10fps each)\n");
    for (int i = 0; i < count; i++) pthread_join(th[i], NULL);

    for (int i = 0; i < count; i++) {
        hbn_vflow_stop(pipes[i].vflow_fd);
        hbn_vflow_destroy(pipes[i].vflow_fd);
        hbn_camera_destroy(pipes[i].cam_fd);
    }
    hb_mem_module_close();
    return 0;
}
