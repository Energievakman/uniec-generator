from flask import Flask, request, send_file, jsonify
import os
import subprocess
import uuid
import re
from pathlib import Path

app = Flask(__name__)
BASE_DIR = Path(__file__).resolve().parent
GENERATOR = BASE_DIR / "generator.py"
TEMPLATE = BASE_DIR / "template.uniec3"


def safe_filename(value: str) -> str:
    value = value.strip().replace(",", "")
    value = re.sub(r"[^A-Za-z0-9_\-\. ]+", "", value)
    value = re.sub(r"\s+", "_", value)
    return value[:80] or "generated"


@app.route("/", methods=["GET"])
def home():
    return jsonify({
        "status": "ok",
        "message": "Uniec generator draait",
        "usage_get": "/generate?address=Vlierboomstraat%20652,%20Den%20Haag",
        "usage_post": {"address": "Vlierboomstraat 652, Den Haag"}
    })


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


@app.route("/generate", methods=["GET", "POST"])
def generate():
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        address = data.get("address")
        height = data.get("height")
        bouwjaar = data.get("bouwjaar")
        pand_id = data.get("pand_id")
        gebruiksoppervlakte = data.get("gebruiksoppervlakte") or data.get("go")
    else:
        address = request.args.get("address")
        height = request.args.get("height")
        bouwjaar = request.args.get("bouwjaar")
        pand_id = request.args.get("pand_id")
        gebruiksoppervlakte = request.args.get("gebruiksoppervlakte") or request.args.get("go")

    if not address:
        return jsonify({"error": "address ontbreekt"}), 400

    out_name = safe_filename(address) + "_" + str(uuid.uuid4()) + ".uniec3"
    output_file = Path("/tmp") / out_name

    cmd = [
        "python3",
        str(GENERATOR),
        "--template", str(TEMPLATE),
        "--address", address,
        "--output", str(output_file),
    ]

    # Optionele handmatige fallbacks, handig voor Softr/Make
    if height not in (None, ""):
        cmd += ["--height", str(height)]
    if bouwjaar not in (None, ""):
        cmd += ["--bouwjaar", str(bouwjaar)]
    if pand_id not in (None, ""):
        cmd += ["--pand-id", str(pand_id)]
    if gebruiksoppervlakte not in (None, ""):
        cmd += ["--gebruiksoppervlakte", str(gebruiksoppervlakte)]

    try:
        result = subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=60)
    except subprocess.CalledProcessError as e:
        return jsonify({
            "error": "generator_failed",
            "returncode": e.returncode,
            "stdout": e.stdout[-4000:] if e.stdout else "",
            "stderr": e.stderr[-4000:] if e.stderr else "",
            "cmd": cmd,
        }), 500
    except Exception as e:
        return jsonify({"error": "server_error", "detail": str(e)}), 500

    if not output_file.exists():
        return jsonify({
            "error": "output_missing",
            "stdout": result.stdout[-4000:] if result.stdout else "",
            "stderr": result.stderr[-4000:] if result.stderr else "",
        }), 500

    download_name = safe_filename(address) + ".uniec3"
    return send_file(output_file, as_attachment=True, download_name=download_name)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
