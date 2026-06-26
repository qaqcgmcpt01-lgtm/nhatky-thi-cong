#!/usr/bin/env python3
"""
app.py — NhậtKý.TC server cho Render.com
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Lưu ảnh: Cloudinary (không mất khi Render restart)
Lưu nhật ký: file JSON trên Render Persistent Disk (mount tại /data)
AI: proxy gọi Claude API (giữ key an toàn phía server)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Biến môi trường cần khai báo trên Render (Environment tab):
  CLAUDE_API_KEY          = sk-ant-...
  CLOUDINARY_CLOUD_NAME   = dlvo5vdwt (ví dụ)
  CLOUDINARY_API_KEY      = ...
  CLOUDINARY_API_SECRET   = ...
"""
import os, json, time, traceback
from flask import Flask, request, jsonify, send_from_directory, Response
import urllib.request
import urllib.error
import cloudinary
import cloudinary.uploader
import cloudinary.api

# ── CẤU HÌNH ──────────────────────────────────────────────────
CLAUDE_KEY = os.environ.get("CLAUDE_API_KEY", "")
DATA_DIR   = os.environ.get("DATA_DIR", "/data")  # Render Persistent Disk mount path

def _setup_logs_dir(base_dir):
    """Tạo thư mục logs; nếu base_dir không có quyền (Disk chưa gắn),
    tự động lùi về thư mục tạm trong code (mất khi redeploy nhưng không crash app)."""
    try:
        target = os.path.join(base_dir, "logs")
        os.makedirs(target, exist_ok=True)
        return target, True
    except PermissionError:
        fallback = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_local_logs")
        os.makedirs(fallback, exist_ok=True)
        print(f"⚠️  Không có quyền ghi vào {base_dir} (Persistent Disk chưa gắn). "
              f"Dùng tạm {fallback} — DỮ LIỆU SẼ MẤT KHI REDEPLOY. "
              f"Vào Render → Disk → Add Disk, mount path = {base_dir}")
        return fallback, False

LOGS_DIR, DISK_OK = _setup_logs_dir(DATA_DIR)

cloudinary.config(
    cloud_name = os.environ.get("CLOUDINARY_CLOUD_NAME", ""),
    api_key    = os.environ.get("CLOUDINARY_API_KEY", ""),
    api_secret = os.environ.get("CLOUDINARY_API_SECRET", ""),
    secure     = True,
)

app = Flask(__name__, static_folder=".", static_url_path="")


# ── TRANG CHỦ — serve app_full.html ──────────────────────────
@app.route("/")
def index():
    return send_from_directory(".", "app_full.html")


@app.route("/<path:filename>")
def static_files(filename):
    # Chỉ serve các file tĩnh đã biết, tránh path traversal
    if filename in ("app_full.html",):
        return send_from_directory(".", filename)
    return jsonify({"error": "Not found"}), 404


# ── WEATHER PROXY (Open-Meteo cho phép CORS nên thực ra FE có thể gọi trực tiếp,
#    nhưng vẫn giữ route này để tương thích nếu cần) ───────────
@app.route("/weather")
def weather():
    date = request.args.get("date", "")
    lat  = request.args.get("lat", "10.776")
    lon  = request.args.get("lon", "106.700")
    url  = (
        f"https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        f"&hourly=temperature_2m,precipitation,weathercode,windspeed_10m"
        f"&timezone=Asia%2FHo_Chi_Minh&start_date={date}&end_date={date}"
    )
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return Response(r.read(), mimetype="application/json")
    except Exception as e:
        return jsonify({"error": str(e)}), 502


# ── CLAUDE API PROXY ──────────────────────────────────────────
@app.route("/claude", methods=["POST"])
def claude_proxy():
    if not CLAUDE_KEY:
        return jsonify({
            "error": "CLAUDE_API_KEY chưa được cấu hình trên Render. "
                     "Vào Render Dashboard → service → Environment → thêm CLAUDE_API_KEY"
        }), 503

    body = request.get_data()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "Content-Type": "application/json",
            "x-api-key": CLAUDE_KEY,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return Response(r.read(), status=r.status, mimetype="application/json")
    except urllib.error.HTTPError as e:
        return Response(e.read(), status=e.code, mimetype="application/json")
    except Exception as e:
        return jsonify({"error": str(e)}), 502


# ── UPLOAD ẢNH — lên Cloudinary ──────────────────────────────
@app.route("/upload", methods=["POST"])
def upload():
    proj = request.args.get("proj", "default")
    date = request.args.get("date", "00000000")

    if "file" not in request.files:
        return jsonify({"ok": False, "error": "Không có file"}), 400

    f = request.files["file"]
    if not f.filename:
        return jsonify({"ok": False, "error": "File rỗng"}), 400

    ext = os.path.splitext(f.filename)[1].lower().lstrip(".")
    if ext not in ["jpg", "jpeg", "png", "gif", "webp", "heic"]:
        return jsonify({"ok": False, "error": f"Không hỗ trợ định dạng .{ext}"}), 400

    if not cloudinary.config().cloud_name:
        return jsonify({
            "ok": False,
            "error": "Cloudinary chưa được cấu hình trên Render (CLOUDINARY_CLOUD_NAME...)"
        }), 503

    try:
        # Dùng public_id đầy đủ đường dẫn — KHÔNG truyền thêm folder để tránh xung đột
        public_id = f"nhatky/{proj}/{date}/{int(time.time()*1000)}"
        result = cloudinary.uploader.upload(
            f,
            public_id=public_id,
            resource_type="image",
            overwrite=True,
        )
        print(f"✅ Upload OK: public_id={result.get('public_id')} url={result.get('secure_url')}")
        return jsonify({
            "ok": True,
            "filename": os.path.basename(result["public_id"]) + "." + result["format"],
            "url": result["secure_url"],
            "size": result.get("bytes", 0),
            "public_id": result["public_id"],
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({"ok": False, "error": str(e)}), 502


# ── LIST ẢNH theo dự án/ngày — đọc từ Cloudinary ─────────────
@app.route("/images")
def list_images():
    proj = request.args.get("proj", "default")
    date = request.args.get("date", "00000000")
    try:
        result = cloudinary.api.resources(
            type="upload",
            resource_type="image",
            prefix=f"nhatky/{proj}/{date}/",
            max_results=200,
        )
        images = [
            {
                "filename": os.path.basename(r["public_id"]) + "." + r["format"],
                "url": r["secure_url"],
                "size": r.get("bytes", 0),
                "public_id": r["public_id"],
            }
            for r in result.get("resources", [])
        ]
        print(f"📷 list_images proj={proj} date={date} → {len(images)} ảnh")
        return jsonify({"ok": True, "images": images})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"ok": True, "images": [], "warning": str(e)})


# ── XÓA ẢNH trên Cloudinary ───────────────────────────────────
@app.route("/img/<proj>/<date>/<filename>", methods=["DELETE"])
def delete_image(proj, date, filename):
    public_id = f"nhatky/{proj}/{date}/{os.path.splitext(filename)[0]}"
    try:
        cloudinary.uploader.destroy(public_id)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 502


# ── LƯU / LOAD NHẬT KÝ — JSON trên Persistent Disk ───────────
@app.route("/save_log", methods=["POST"])
def save_log():
    data = request.get_json(force=True, silent=True)
    if not data:
        return jsonify({"ok": False, "error": "Invalid JSON"}), 400

    proj = "".join(c for c in str(data.get("project", "default")) if c.isalnum() or c in "-_")
    date = "".join(c for c in str(data.get("date", "")) if c.isdigit())
    if not date:
        return jsonify({"ok": False, "error": "Missing date"}), 400

    proj_dir = os.path.join(LOGS_DIR, proj)
    os.makedirs(proj_dir, exist_ok=True)
    with open(os.path.join(proj_dir, date + ".json"), "w", encoding="utf-8") as fp:
        json.dump(data, fp, ensure_ascii=False, indent=2)

    return jsonify({"ok": True})


@app.route("/load_log")
def load_log():
    proj = request.args.get("proj", "default")
    date = request.args.get("date", "")
    fpath = os.path.join(LOGS_DIR, proj, date + ".json")
    if os.path.exists(fpath):
        with open(fpath, "r", encoding="utf-8") as fp:
            return jsonify({"ok": True, "log": json.load(fp)})
    return jsonify({"ok": True, "log": None})


# ── HEALTH CHECK (Render dùng để biết service còn sống) ──────
@app.route("/healthz")
def healthz():
    return jsonify({
        "status": "ok",
        "claude_configured": bool(CLAUDE_KEY),
        "cloudinary_configured": bool(cloudinary.config().cloud_name),
        "persistent_disk_ok": DISK_OK,
        "logs_dir": LOGS_DIR,
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
