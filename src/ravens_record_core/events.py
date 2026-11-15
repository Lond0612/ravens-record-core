"""
イベント名（logger.emit の event。JSON Lines の "event"）

どのイベントにも ts・level・event・message が付く。下はそれぞれに足される主な項目。
msg_key は、画面に出すときの文言のキー（使う側のアプリが、画面の言語で表示するため。message はログ用の日本語）。
"""

LOG = "log"                                  # ふつうのログ 1 行
STATUS = "status"                            # 監視状態の変化（state・msg_key）

# ランクマッチ
MATCH_DETECTED = "match_detected"            # 対戦の開始を見つけた
MAP_DETECTED = "map_detected"                # マップを判定した（map・confidence。判定できなければ map は null）
SCORE_UPDATED = "score_updated"              # 試合中のスコアが変わった（子プロセス ScoreDetector から）
SCORE_CONFIRMED = "score_confirmed"          # 最終スコアが決まった（子プロセス ScoreDetector から）
SCORE_MISSING = "score_missing"              # スコアを確定できないまま試合が終わった
MATCH_SAVED = "match_saved"                  # 試合を保存した（result・path）

# ロード画面の監視（子プロセス）からの知らせ。phase: started / loading_screen / loading_end / intro_start / timeout
LOADING_WATCH = "loading_watch"

# カスタムマッチ
CUSTOM_MATCH_DETECTED = "custom_match_detected"          # 対戦の開始を見つけた（map）
CUSTOM_MATCH_SAVED = "custom_match_saved"                # 自分が出た対戦を保存した（own_wins・opp_wins）
CUSTOM_SPECTATED_SAVED = "custom_spectated_saved"        # 観戦した対戦を保存した（alpha_wins・beta_wins）
CUSTOM_SPECTATED_SKIPPED = "custom_spectated_skipped"    # 観戦した対戦の結果を最後まで読めず、保存しなかった

# status の state
STATE_WAITING_READY = "waiting_ready"        # 記録を始められるようになるまで待っている（Hooks.is_ready が False）
STATE_WAITING_AC6 = "waiting_ac6"            # ゲームの起動を待っている
STATE_AC6_DETECTED = "ac6_detected"          # ゲームを見つけた（ロビーを待っている）
STATE_MATCHMAKING = "matchmaking"            # ロビー・カスタムマッチのルームで、対戦の開始を待っている
STATE_LOADING = "loading"                    # 対戦を見つけ、ロード中
STATE_MAP_DETECTION = "map_detection"        # マップを判定中
STATE_IN_MATCH = "in_match"                  # 対戦中（スコアを監視している）
STATE_POST_MATCH = "post_match"              # 試合が終わり、結果を保存している
