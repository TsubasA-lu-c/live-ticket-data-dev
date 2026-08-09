"""ライブ情報の機械収集パイプライン。

AIに公式サイトを巡回させる従来方式を置き換える。
処理順は fetcher → normalize → diff → extract → merge で、
機械的に解決できなかったものだけを ai_queue に積む（Web巡回はさせない）。
"""
