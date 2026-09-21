"""
Discord botで使うLLMシステムプロンプトとプロンプト生成関数を集約するスクリプト。

【概要】
ファイル肥大化のため、実際の定義は `text/` フォルダ内のモジュールに分割されました。
このファイルは後方互換性のために、分割されたモジュールからすべての定義をインポートし、
再エクスポートするアグリゲーターとしての役割のみを持ちます。
"""

from .text.analysis_texts import *
from .text.reply_texts import *
from .text.reply_texts import _slim_base_prompt_for_retry
