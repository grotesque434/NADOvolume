import csv
from datetime import datetime
from pathlib import Path

from config import load_settings
from contws_bot import ContwsBot
from sheets_client import SheetsClient


def append_log(
    profile_id: str, action: str, source_row: int, ok: bool, external_ip: str = ""
) -> None:
    log_path = Path("run_log.csv")
    is_new = not log_path.exists()
    with log_path.open("a", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        if is_new:
            writer.writerow(
                ["timestamp", "profile_id", "action", "source_row", "ok", "external_ip"]
            )
        writer.writerow(
            [datetime.utcnow().isoformat(), profile_id, action, source_row, ok, external_ip]
        )


def main() -> None:
    settings = load_settings()
    sheets = SheetsClient(settings.google_sheets_id, settings.google_service_account_json)
    bot = ContwsBot(headless=settings.headless)
    base_profiles_dir = Path(settings.profiles_dir)
    base_profiles_dir.mkdir(parents=True, exist_ok=True)

    for profile_id in settings.profiles:
        print(f"\n=== Profile {profile_id} ===")
        posts = sheets.get_posts_for_profile(profile_id)
        print(f"[{profile_id}] Pending posts from sheet: {len(posts)}")
        user_data_dir = str(base_profiles_dir / profile_id)
        proxy = settings.profile_proxies.get(profile_id)
        if proxy:
            print(f"[{profile_id}] Proxy enabled: {proxy.server}")
        else:
            print(f"[{profile_id}] Proxy disabled")

        try:
            external_ip, results = bot.run_profile(
                profile_id=profile_id,
                user_data_dir=user_data_dir,
                target_profile_url=settings.target_profile_url,
                posts=posts,
                proxy=proxy,
            )
        except Exception as exc:
            print(f"[{profile_id}] Stopped with error: {exc}")
            append_log(profile_id, "profile_run", -1, False)
            continue

        append_log(profile_id, "ip_check", -1, True, external_ip)

        for source_row, ok in results:
            append_log(profile_id, "publish", source_row, ok, external_ip)
            if ok:
                sheets.mark_done(source_row)
                print(f"[{profile_id}] Row {source_row} marked as done.")


if __name__ == "__main__":
    main()
