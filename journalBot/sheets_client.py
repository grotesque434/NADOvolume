from dataclasses import dataclass

import gspread
from google.oauth2.service_account import Credentials


@dataclass
class PostRow:
    profile_id: str
    title: str
    body: str
    status: str
    source_row: int


class SheetsClient:
    def __init__(self, spreadsheet_id: str, service_account_json: str) -> None:
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive.readonly",
        ]
        creds = Credentials.from_service_account_file(service_account_json, scopes=scopes)
        self._client = gspread.authorize(creds)
        self._spreadsheet = self._client.open_by_key(spreadsheet_id)
        self._worksheet = self._spreadsheet.worksheet("Posts")

    def get_posts_for_profile(self, profile_id: str) -> list[PostRow]:
        rows = self._worksheet.get_all_records()
        out: list[PostRow] = []
        for idx, row in enumerate(rows, start=2):
            row_profile = str(row.get("Profile ID", "")).strip()
            if row_profile != profile_id:
                continue

            status = str(row.get("Status", "")).strip().lower()
            if status in {"done", "posted", "skip"}:
                continue

            out.append(
                PostRow(
                    profile_id=row_profile,
                    title=str(row.get("Title", "")).strip(),
                    body=str(row.get("Body", "")).strip(),
                    status=status,
                    source_row=idx,
                )
            )
        return out

    def mark_done(self, source_row: int) -> None:
        status_col = self._column_index("Status")
        self._worksheet.update_cell(source_row, status_col, "done")

    def _column_index(self, column_name: str) -> int:
        headers = self._worksheet.row_values(1)
        for i, header in enumerate(headers, start=1):
            if header.strip().lower() == column_name.strip().lower():
                return i
        raise ValueError(f"Column not found: {column_name}")
