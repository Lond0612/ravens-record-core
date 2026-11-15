"""日時の小道具"""

from datetime import datetime, timezone
from typing import Optional


def parse_iso_utc(value) -> Optional[datetime]:
    """ISO 8601 の文字列を、タイムゾーン付きの日時にする（タイムゾーンが無ければ UTC とみなす。読めなければ None）"""
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)
