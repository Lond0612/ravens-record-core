"""
試合の監視（ランクマッチ・カスタムマッチ）

AC6 の起動を見つけ、画面を読みながら、次の順に進める:
  ロビーで対戦の開始を待つ（match_start）→ ロードの終わりを待つ（loading）→ マップを判定する（map_detection）→
  スコアを監視する（children: ScoreDetector）→ 結果を保存する（post_match）
全体の流れは loop.py、カスタムマッチは custom.py。状態は MonitorContext（base.py）が持つ。
"""
