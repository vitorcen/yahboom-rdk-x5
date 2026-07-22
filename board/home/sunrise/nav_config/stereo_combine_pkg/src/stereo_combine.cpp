/*
 * stereo_combine — C++ hot path for the GS130WI stereo pipeline.
 *
 * Replaces stereo_cam.py's tick_combine/tick_color threads, which shared one
 * python GIL and capped the whole pipeline around 11 iterations/s combined.
 *
 *   /dev/shm/stereo_cam{0,1}.nv12 (stereo_capture, 60fps)
 *     -> combine thread: fixed-point remap (rectify+crop+scale) both eyes,
 *        [Y_L][Y_R][UV_L][UV_R] 640x704 nv12 -> /image_combine_raw
 *     -> color thread: eye0 downscale + radial chroma fix (no IR-cut module,
 *        see docs/gs130wi-stereo-camera-bringup.html §6.5) + JPEG -> /image_jpeg
 *
 * Rectify maps are precomputed by stereo_cam.py (EEPROM calib) and exported to
 * rect_maps.bin; this node only consumes them. stereo_cam.py spawns and
 * watchdogs this process alongside stereo_capture/stereonet/codec.
 */
#include <fcntl.h>
#include <sys/mman.h>
#include <unistd.h>

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <deque>
#include <fstream>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

#include <opencv2/core.hpp>
#include <opencv2/imgcodecs.hpp>
#include <opencv2/imgproc.hpp>
#include <ai_msgs/msg/perception_targets.hpp>
#include <rclcpp/rclcpp.hpp>
#include <sensor_msgs/msg/compressed_image.hpp>
#include <sensor_msgs/msg/image.hpp>
#include <std_msgs/msg/float32_multi_array.hpp>

static constexpr int W = 1088, H = 1280;      // sensor native (portrait)
static constexpr int MW = 640, MH = 352;      // stereonet model input per eye
static constexpr int OW = 544, OH = 640;      // color preview
static constexpr int CHROMA_BINS = 24;
static constexpr float CHROMA_SAT = 1.6f;
static constexpr int JPEG_Q = 80;

static const char *PAIR_PATH = "/dev/shm/stereo_pair.shm";
static const char *MAPS_BIN = "/home/sunrise/nav_config/rect_maps.bin";
static constexpr size_t FRAME_BYTES = (size_t)W * H * 3 / 2;
static constexpr size_t PAIR_HDR = 64;

static int env_int(const char *name, int dflt) {
    const char *e = getenv(name);
    return e ? atoi(e) : dflt;
}

/* ---- paired frames via stereo_capture's seqlocked mmap ----
 * The daemon owns both eyes and pairs at the source (per-eye files + reader
 * side ts-matching collapsed under load: independent ~13% frame drops broke
 * the near-pair guarantee). Layout: see stereo_capture.c. */
class PairReader {
public:
    bool open_map() {
        int fd = open(PAIR_PATH, O_RDONLY);
        if (fd < 0) return false;
        map_ = (const uint8_t *)mmap(nullptr, PAIR_HDR + 2 * FRAME_BYTES,
                                     PROT_READ, MAP_SHARED, fd, 0);
        close(fd);
        if (map_ == MAP_FAILED) { map_ = nullptr; return false; }
        return memcmp(map_, "STPR", 4) == 0;
    }
    /* Cheap header peek: is there a pair newer than last_ts? */
    bool has_new(double last_ts) {
        if (!map_ && !open_map()) return false;
        uint32_t s = __atomic_load_n((const uint32_t *)(map_ + 4), __ATOMIC_ACQUIRE);
        if (s == 0 || (s & 1)) return false;
        uint64_t t0, t1;
        memcpy(&t0, map_ + 16, 8);
        memcpy(&t1, map_ + 24, 8);
        return std::max(t0, t1) / 1e9 > last_ts;
    }

    /* Copy a consistent snapshot; eyes==1 copies eye0 only (color path).
     * Returns pair ts (s) or 0 when no new pair vs last_ts. */
    double snap(uint8_t *e0, uint8_t *e1, double last_ts) {
        if (!map_ && !open_map()) return 0.0;
        auto *seq = (const uint32_t *)(map_ + 4);
        for (int retry = 0; retry < 4; retry++) {
            uint32_t s1 = __atomic_load_n(seq, __ATOMIC_ACQUIRE);
            if (s1 == 0 || (s1 & 1)) { usleep(500); continue; }
            uint64_t t0, t1;
            memcpy(&t0, map_ + 16, 8);
            memcpy(&t1, map_ + 24, 8);
            double ts = std::max(t0, t1) / 1e9;
            if (ts <= last_ts) return 0.0;    // nothing new
            memcpy(e0, map_ + PAIR_HDR, FRAME_BYTES);
            if (e1) memcpy(e1, map_ + PAIR_HDR + FRAME_BYTES, FRAME_BYTES);
            uint32_t s2 = __atomic_load_n(seq, __ATOMIC_ACQUIRE);
            if (s1 == s2) return ts;          // consistent snapshot
        }
        return 0.0;
    }

private:
    const uint8_t *map_ = nullptr;
};

/* ---- rect maps (exported by stereo_cam.py, float32; converted to
 *      fixed-point CV_16SC2 here for the fast remap path) ---- */
struct EyeMaps {
    cv::Mat y1, y2, uv1, uv2;                 // convertMaps output pairs
};

static bool load_maps(EyeMaps m[2]) {
    std::ifstream in(MAPS_BIN, std::ios::binary);
    if (!in) return false;
    char magic[5] = {0};
    uint32_t mw = 0, mh = 0;
    in.read(magic, 5);
    in.read((char *)&mw, 4);
    in.read((char *)&mh, 4);
    if (memcmp(magic, "RMAP1", 5) != 0 || mw != MW || mh != MH) return false;
    auto rd = [&](int rows, int cols) {
        cv::Mat a(rows, cols, CV_32FC1);
        in.read((char *)a.data, (std::streamsize)(a.total() * 4));
        return a;
    };
    for (int e = 0; e < 2; e++) {
        cv::Mat yx = rd(MH, MW), yy = rd(MH, MW);
        cv::Mat ux = rd(MH / 2, MW / 2), uy = rd(MH / 2, MW / 2);
        if (!in) return false;
        cv::convertMaps(yx, yy, m[e].y1, m[e].y2, CV_16SC2);
        cv::convertMaps(ux, uy, m[e].uv1, m[e].uv2, CV_16SC2);
    }
    return true;
}

/* ---- fps overlay (user-visible truth, same style as the python node) ---- */
struct FpsMeter {
    std::deque<double> q;
    void stamp(cv::Mat &bgr) {
        double now = std::chrono::duration<double>(
            std::chrono::steady_clock::now().time_since_epoch()).count();
        q.push_back(now);
        while (!q.empty() && now - q.front() > 3.0) q.pop_front();
        double fps = q.size() > 1 ? (q.size() - 1) / (now - q.front()) : 0.0;
        char txt[32];
        snprintf(txt, sizeof(txt), "%.1ffps", fps);
        // single-channel (Y plane) gets white-on-black; BGR gets green
        cv::Scalar fg = bgr.channels() == 1 ? cv::Scalar(255) : cv::Scalar(0, 255, 80);
        cv::putText(bgr, txt, {8, 24}, cv::FONT_HERSHEY_SIMPLEX, 0.7, {0, 0, 0}, 4);
        cv::putText(bgr, txt, {8, 24}, cv::FONT_HERSHEY_SIMPLEX, 0.7, fg, 2);
    }
};

/* ---- radial chroma correction (port of stereo_cam.py chroma_fix) ---- */
struct ChromaFix {
    std::vector<uint8_t> ring;                // per-pixel ring index (OH/2 x OW/2)
    std::vector<int> cnt;
    std::vector<float> up, vp;
    bool primed = false;

    ChromaFix() {
        int h = OH / 2, w = OW / 2;
        ring.resize((size_t)h * w);
        cnt.assign(CHROMA_BINS, 0);
        float rmax = std::sqrt((h / 2.f) * (h / 2.f) + (w / 2.f) * (w / 2.f));
        for (int y = 0; y < h; y++)
            for (int x = 0; x < w; x++) {
                float r = std::sqrt((y - h / 2.f) * (y - h / 2.f) +
                                    (x - w / 2.f) * (x - w / 2.f));
                int b = std::min((int)(r / rmax * CHROMA_BINS), CHROMA_BINS - 1);
                ring[(size_t)y * w + x] = (uint8_t)b;
                cnt[b]++;
            }
        up.assign(CHROMA_BINS, 0.f);
        vp.assign(CHROMA_BINS, 0.f);
    }

    void apply(cv::Mat &uv) {                 // CV_8UC2, (OH/2 x OW/2)
        double su[CHROMA_BINS] = {0}, sv[CHROMA_BINS] = {0};
        int h = uv.rows, w = uv.cols;
        for (int y = 0; y < h; y++) {
            const uint8_t *p = uv.ptr<uint8_t>(y);
            const uint8_t *rg = &ring[(size_t)y * w];
            for (int x = 0; x < w; x++) {
                su[rg[x]] += p[2 * x] - 128;
                sv[rg[x]] += p[2 * x + 1] - 128;
            }
        }
        float nu[CHROMA_BINS], nv[CHROMA_BINS];
        for (int b = 0; b < CHROMA_BINS; b++) {
            int b0 = std::max(0, b - 1), b1 = std::min(CHROMA_BINS - 1, b + 1);
            double cu = 0, cv2_ = 0;
            int cc = 0;
            for (int k = b0; k <= b1; k++) { cu += su[k]; cv2_ += sv[k]; cc += cnt[k]; }
            nu[b] = (float)(cu / cc);
            nv[b] = (float)(cv2_ / cc);
        }
        for (int b = 0; b < CHROMA_BINS; b++) {
            if (!primed) { up[b] = nu[b]; vp[b] = nv[b]; }
            else { up[b] += 0.3f * (nu[b] - up[b]); vp[b] += 0.3f * (nv[b] - vp[b]); }
        }
        primed = true;
        for (int y = 0; y < h; y++) {
            uint8_t *p = uv.ptr<uint8_t>(y);
            const uint8_t *rg = &ring[(size_t)y * w];
            for (int x = 0; x < w; x++) {
                float u = ((p[2 * x] - 128) - up[rg[x]]) * CHROMA_SAT + 128.f;
                float v = ((p[2 * x + 1] - 128) - vp[rg[x]]) * CHROMA_SAT + 128.f;
                p[2 * x] = (uint8_t)std::clamp(u, 0.f, 255.f);
                p[2 * x + 1] = (uint8_t)std::clamp(v, 0.f, 255.f);
            }
        }
    }
};

class StereoCombine : public rclcpp::Node {
public:
    StereoCombine() : Node("stereo_combine") {
        pub_combine_ = create_publisher<sensor_msgs::msg::Image>("/image_combine_raw", 2);
        pub_color_ = create_publisher<sensor_msgs::msg::Image>("/image_color_nv12", 2);
        // native-res eye0 for the BPU perception chain: at the 544 preview
        // scale a hand is ~40px and the gesture classifier mostly returns 0;
        // at 1088x1280 it gets ~80px and works. Lazy: only when subscribed.
        pub_full_ = create_publisher<sensor_msgs::msg::Image>("/image_color_full", 2);
        if (!load_maps(maps_)) {
            RCLCPP_FATAL(get_logger(), "cannot load %s", MAPS_BIN);
            throw std::runtime_error("maps");
        }
        combine_hz_ = env_int("COMBINE_HZ", 25);
        color_hz_ = env_int("COLOR_HZ", 0);    // 0 = event-driven, every new pair
        perc_hz_ = env_int("PERC_HZ", 10);     // native-res feed for perception
        RCLCPP_INFO(get_logger(), "maps ok; combine %dHz color %dHz", combine_hz_, color_hz_);
        // follow-me range assist (idle unless the perception chain is up)
        pub_ranges_ = create_publisher<std_msgs::msg::Float32MultiArray>(
            "/follow/cam_ranges", 5);
        sub_targets_ = create_subscription<ai_msgs::msg::PerceptionTargets>(
            "/hobot_mono2d_body_detection", 5,
            [this](ai_msgs::msg::PerceptionTargets::SharedPtr m) { on_targets(m); });
        sub_depth_ = create_subscription<sensor_msgs::msg::Image>(
            "/StereoNetNode/stereonet_depth", rclcpp::SensorDataQoS(),
            [this](sensor_msgs::msg::Image::SharedPtr m) { on_depth(m); });
        th_combine_ = std::thread([this] { loop_combine(); });
        th_color_ = std::thread([this] { loop_color(); });
    }
    ~StereoCombine() override {
        run_ = false;
        if (th_combine_.joinable()) th_combine_.join();
        if (th_color_.joinable()) th_color_.join();
    }

private:
    void loop_combine() {
        PairReader pr;
        std::vector<uint8_t> e0(FRAME_BYTES), e1(FRAME_BYTES);
        sensor_msgs::msg::Image msg;
        msg.header.frame_id = "stereo";
        msg.height = MH * 2;                  // logical Y rows; UV rows implied
        msg.width = MW;
        msg.encoding = "nv12";
        msg.step = MW;
        msg.data.resize((size_t)MW * MH * 3); // YL YR UVL UVR
        const auto period = std::chrono::duration<double>(1.0 / combine_hz_);
        int ok = 0, tries = 0;
        double last_ts = 0.0;
        auto t_rep = std::chrono::steady_clock::now();

        while (run_ && rclcpp::ok()) {
            auto t0 = std::chrono::steady_clock::now();
            tries++;
            double ts = pr.snap(e0.data(), e1.data(), last_ts);
            if (ts > 0.0) {
                last_ts = ts;
                uint8_t *d = msg.data.data();
                cv::Mat yl(MH, MW, CV_8UC1, d);
                cv::Mat yr(MH, MW, CV_8UC1, d + (size_t)MW * MH);
                cv::Mat uvl(MH / 2, MW / 2, CV_8UC2, d + (size_t)MW * MH * 2);
                cv::Mat uvr(MH / 2, MW / 2, CV_8UC2, d + (size_t)MW * MH * 5 / 2);
                cv::Mat srcyl(H, W, CV_8UC1, e0.data()), srcyr(H, W, CV_8UC1, e1.data());
                cv::Mat srcuvl(H / 2, W / 2, CV_8UC2, e0.data() + (size_t)W * H);
                cv::Mat srcuvr(H / 2, W / 2, CV_8UC2, e1.data() + (size_t)W * H);
                cv::remap(srcyl, yl, maps_[0].y1, maps_[0].y2, cv::INTER_LINEAR);
                cv::remap(srcyr, yr, maps_[1].y1, maps_[1].y2, cv::INTER_LINEAR);
                cv::remap(srcuvl, uvl, maps_[0].uv1, maps_[0].uv2, cv::INTER_LINEAR);
                cv::remap(srcuvr, uvr, maps_[1].uv1, maps_[1].uv2, cv::INTER_LINEAR);

                double stamp = ts;
                if (stamp <= last_stamp_) stamp = last_stamp_ + 1e-4;  // stereonet drops non-increasing
                last_stamp_ = stamp;
                msg.header.stamp.sec = (int32_t)stamp;
                msg.header.stamp.nanosec = (uint32_t)((stamp - (int64_t)stamp) * 1e9);
                pub_combine_->publish(msg);
                ok++;
            }
            if (std::chrono::steady_clock::now() - t_rep > std::chrono::seconds(10)) {
                RCLCPP_INFO(get_logger(), "combine: ok=%d/%d in 10s", ok, tries);
                ok = tries = 0;
                t_rep = std::chrono::steady_clock::now();
            }
            std::this_thread::sleep_until(t0 + period);
        }
    }

    /* Event-driven: process every new pair (no rate cap by default). JPEG
     * moved off-CPU to the VPU JENC via a second hobot_codec instance
     * (channel 2) subscribing /image_color_nv12 — software imencode was
     * ~30ms/frame of pure CPU that fought the depth chain for cores. */
    void loop_color() {
        PairReader pr;
        std::vector<uint8_t> e0(FRAME_BYTES);
        ChromaFix cf;
        FpsMeter fps;
        double last_ts = 0.0;
        const auto min_period = std::chrono::duration<double>(
            color_hz_ > 0 ? 1.0 / color_hz_ : 0.0);
        sensor_msgs::msg::Image msg;
        msg.header.frame_id = "sc132gs_color";
        msg.height = OH;
        msg.width = OW;
        msg.encoding = "nv12";
        msg.step = OW;
        msg.data.resize((size_t)OW * OH * 3 / 2);
        sensor_msgs::msg::Image full;
        full.header.frame_id = "sc132gs_color";
        full.height = H;
        full.width = W;
        full.encoding = "nv12";
        full.step = W;
        full.data.resize(FRAME_BYTES);
        double last_full = 0.0;

        while (run_ && rclcpp::ok()) {
            if (!pr.has_new(last_ts)) {
                std::this_thread::sleep_for(std::chrono::milliseconds(4));
                continue;
            }
            auto t0 = std::chrono::steady_clock::now();
            double ts = pr.snap(e0.data(), nullptr, last_ts);
            if (ts > 0.0) {
                last_ts = ts;
                if (pub_full_->get_subscription_count() > 0 && perc_hz_ > 0
                        && ts - last_full >= 1.0 / perc_hz_) {
                    last_full = ts;
                    memcpy(full.data.data(), e0.data(), FRAME_BYTES);
                    full.header.stamp = now();
                    pub_full_->publish(full);
                }
                cv::Mat y(H, W, CV_8UC1, e0.data());
                cv::Mat uv(H / 2, W / 2, CV_8UC2, e0.data() + (size_t)W * H);
                cv::Mat ys(OH, OW, CV_8UC1, msg.data.data());
                cv::Mat uvs(OH / 2, OW / 2, CV_8UC2, msg.data.data() + (size_t)OW * OH);
                cv::resize(y, ys, ys.size(), 0, 0, cv::INTER_AREA);
                cv::resize(uv, uvs, uvs.size(), 0, 0, cv::INTER_AREA);
                cf.apply(uvs);
                fps.stamp(ys);                // Y-plane overlay (pre-encode)
                msg.header.stamp = now();
                pub_color_->publish(msg);
            }
            if (color_hz_ > 0) std::this_thread::sleep_until(t0 + min_period);
        }
    }

    /* ---- follow-me range assist: body rois (mono2d on the native-res
     * /image_color_full stream) x stereonet depth (rectified eye0 frame, mm).
     * maps_[0].y1 (CV_16SC2) already gives every rectified pixel its source
     * pixel, so "which depth pixels fall in this roi" is one flat sweep — no
     * inverse rectification needed. Output = flat [id, cx, range_m] triplets
     * (cx in native px), tiny enough for rclpy to subscribe safely. */
    struct Body { int id; cv::Rect rect; };            // rect in native px

    void on_targets(const ai_msgs::msg::PerceptionTargets::SharedPtr &m) {
        std::vector<Body> v;
        for (const auto &t : m->targets)
            for (const auto &roi : t.rois)
                if (roi.type == "body")
                    v.push_back({(int)t.track_id,
                                 {(int)roi.rect.x_offset, (int)roi.rect.y_offset,
                                  (int)roi.rect.width, (int)roi.rect.height}});
        std::lock_guard<std::mutex> lk(mu_bodies_);
        bodies_ = std::move(v);
        bodies_t_ = std::chrono::steady_clock::now();
    }

    void on_depth(const sensor_msgs::msg::Image::SharedPtr &m) {
        std::vector<Body> bodies;
        {
            std::lock_guard<std::mutex> lk(mu_bodies_);
            if (bodies_.empty() || std::chrono::steady_clock::now() - bodies_t_
                    > std::chrono::milliseconds(600))
                return;                                // perception idle/stale
            bodies = bodies_;
        }
        if (m->encoding != "mono16" || (int)m->width != MW || (int)m->height != MH)
            return;
        const auto *dp = (const uint16_t *)m->data.data();
        std::vector<std::vector<uint16_t>> hits(bodies.size());
        for (int y = 0; y < MH; y++) {
            const int16_t *mp = maps_[0].y1.ptr<int16_t>(y);
            for (int x = 0; x < MW; x++) {
                uint16_t d = dp[y * MW + x];
                if (!d) continue;
                cv::Point sp(mp[2 * x], mp[2 * x + 1]);
                for (size_t i = 0; i < bodies.size(); i++)
                    if (bodies[i].rect.contains(sp)) hits[i].push_back(d);
            }
        }
        std_msgs::msg::Float32MultiArray out;
        for (size_t i = 0; i < bodies.size(); i++) {
            auto &h = hits[i];
            if (h.size() < 200) continue;              // occluded / off-frame
            // 30th percentile: the person is the foreground inside their box
            auto nth = h.begin() + h.size() * 3 / 10;
            std::nth_element(h.begin(), nth, h.end());
            out.data.push_back((float)bodies[i].id);
            out.data.push_back(bodies[i].rect.x + bodies[i].rect.width / 2.0f);
            out.data.push_back(*nth / 1000.0f);
        }
        if (!out.data.empty()) pub_ranges_->publish(out);
    }

    rclcpp::Publisher<sensor_msgs::msg::Image>::SharedPtr pub_combine_;
    rclcpp::Publisher<sensor_msgs::msg::Image>::SharedPtr pub_color_;
    rclcpp::Publisher<sensor_msgs::msg::Image>::SharedPtr pub_full_;
    rclcpp::Publisher<std_msgs::msg::Float32MultiArray>::SharedPtr pub_ranges_;
    rclcpp::Subscription<ai_msgs::msg::PerceptionTargets>::SharedPtr sub_targets_;
    rclcpp::Subscription<sensor_msgs::msg::Image>::SharedPtr sub_depth_;
    std::vector<Body> bodies_;
    std::chrono::steady_clock::time_point bodies_t_{};
    std::mutex mu_bodies_;
    EyeMaps maps_[2];
    std::thread th_combine_, th_color_;
    std::atomic<bool> run_{true};
    double last_stamp_ = 0.0;
    int combine_hz_ = 25, color_hz_ = 12, perc_hz_ = 10;
};

int main(int argc, char **argv) {
    rclcpp::init(argc, argv);
    auto node = std::make_shared<StereoCombine>();
    rclcpp::spin(node);                       // serves the range-assist subs
    rclcpp::shutdown();
    return 0;
}
