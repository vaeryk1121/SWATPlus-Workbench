"""Start the portable distribution using its own interpreter and saved environment."""
import argparse
import json
import os
from pathlib import Path
import sys
import traceback

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--configure", action="store_true")
    parser.add_argument("--check-environment", action="store_true")
    parser.add_argument("--diagnose", action="store_true")
    parser.add_argument("--smoke-test", action="store_true")
    args, remaining = parser.parse_known_args()
    os.chdir(ROOT)
    if Path(sys.prefix).resolve() == (ROOT.parent / "runtime").resolve():
        os.environ["PYTHONNOUSERSITE"] = "1"
    # Avoid per-user site packages affecting the bundled runtime.
    sys.dont_write_bytecode = True
    from portable_runtime import CONFIG, discover, read_config, save, validate
    if args.diagnose:
        cfg = discover(read_config())
        errors = validate(cfg, probe=True)
        print(json.dumps({"python": sys.executable, "root": str(ROOT),
                          "environment": cfg, "errors": errors}, ensure_ascii=False, indent=2))
        return int(bool(errors))
    if args.smoke_test:
        import tkinter as tk
        from desktop_workbench import Desktop, enable_high_dpi
        enable_high_dpi()
        window = tk.Tk()
        desktop = Desktop(window)
        window.update()
        desktop.show_page("history")
        window.update()
        desktop.show_page("data")
        window.update()
        window.destroy()
        print("Desktop data and history pages opened successfully.")
        return 0
    cfg = read_config()
    if args.check_environment:
        cfg = discover(cfg)
        errors = validate(cfg, probe=True)
        if errors:
            print("\n".join(errors))
            return 1
        save(cfg)
        print("Environment configured successfully.")
        return 0
    # A valid saved environment is reused. A move or a removed dependency opens setup.
    if args.configure or validate(cfg):
        from desktop_environment import configure_environment
        if not configure_environment(initial=cfg, automatic=not args.configure):
            return 0
    sys.argv = [str(ROOT / "scripts/swatplus_desktop.py"), *remaining]
    from desktop_workbench import main as desktop_main
    desktop_main()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        detail = traceback.format_exc()
        (ROOT / "startup-error.log").write_text(detail, encoding="utf-8")
        import tkinter.messagebox
        tkinter.messagebox.showerror("SWAT+ Workbench 启动失败",
                                    detail[-1600:] + "\n详细信息已保存到 startup-error.log")
        raise
