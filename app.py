
# In your Flask app:
from flask import flash

@app.route("/config", methods=["GET", "POST"])
def config_editor():
    if request.method == "POST":
        # Respect scalar vs complex inputs:
        # If a field looks like JSON/YAML for complex types, allow it; otherwise keep as string.
        new_config = {}
        for k, v in request.form.items():
            # Try to parse into Python structure if it looks complex
            try:
                # Prefer YAML to allow comments & broader syntax
                parsed = yaml.safe_load(v)
                new_config[k] = parsed
            except Exception:
                new_config[k] = v  # leave as string if not parseable

        with open(CONFIG_PATH, "w") as f:
            yaml.safe_dump(new_config, f, sort_keys=False)

        flash("Configuration saved.")
        return redirect(url_for("dashboard"))
    else:
        with open(CONFIG_PATH) as f:
            config = yaml.safe_load(f) or {}
        return render_template("config.html", config=config)
