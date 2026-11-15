"""
レートとしてありうる範囲（読み誤りの除外に使う）

運営者確認: 3000 以上はありえない。1150 未満もまずいないため、読み誤りとみなす（2026-10-29 に 1000 → 1150）。
範囲外の値は、読み誤り（桁の欠け・別の数字）として、読めなかった扱いにする。
"""

RATE_MIN = 1150
RATE_MAX = 2999


def is_plausible_rate(rate) -> bool:
    """レートとしてありうる整数か（None・bool・範囲外は False）"""
    return isinstance(rate, int) and not isinstance(rate, bool) and RATE_MIN <= rate <= RATE_MAX
