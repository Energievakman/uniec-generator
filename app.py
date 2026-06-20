from flask import Flask, request, send_file, jsonify
import subprocess
import uuid
import os

app = Flask(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

@app.route("/")
def home():
    return "Uniec generator draait"

@app.route("/generate", methods=["GET", "POST"])
def generate():
    if request.method == "POST":
        data = request.get_json() or {}
        address = data.get("address")
    else:
        address = request.args.get("address")

    if not address:
        return jsonify({"error": "address ontbreekt"}), 400

    safe_name = address.replace(",", "").replace(" ", "_")
    output_file = f"/tmp/{safe_name}_{uuid.uuid4()}.uniec3"

    subprocess.run([
        "python3",
        os.path.join(BASE_DIR, "generator.py"),
        "--template", os.path.join(BASE_DIR, "template.uniec3"),
        "--address", address,
        "--output", output_file
    ], check=True)

    return send_file(
        output_file,
        as_attachment=True,
        download_name=f"{address}.uniec3"
    )