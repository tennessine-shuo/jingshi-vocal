import pathlib

SHADOW = "\\\\?\\GLOBALROOT\\Device\\HarddiskVolumeShadowCopy46\\"
files = {
    "cloud_config": SHADOW + "recorder\\cloud\\cloud_config.json",
    "recorder_config": SHADOW + "recorder\\recorder\\config.json",
}
outdir = pathlib.Path(r"C:\Users\汤朔\AppData\Local\Temp\rc_restore")
outdir.mkdir(parents=True, exist_ok=True)
for label, p in files.items():
    try:
        data = open(p, "rb").read()
        out = outdir / (label + ".json")
        out.write_bytes(data)
        print(f"  OK  {label}: {len(data)} bytes -> {out}")
    except OSError as e:
        print(f"  FAIL {label}: {e}")
