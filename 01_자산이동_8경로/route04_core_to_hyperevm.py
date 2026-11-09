"""경로 4: HyperCore HYPE 전송과 동일 주소의 HyperEVM System TX를 연결한다.

이 파일은 전송을 만들지 않는다. 공개 HyperCore 원장과 공식 HyperEVM의
``eth_getSystemTxsByBlockNumber`` 결과만 읽는다. Core action hash와 System TX
hash가 다르므로 주소, 자산, 수량, 시각으로 후보를 만들고 하나일 때만
``UNIQUE_MATCH``로 판정한다.
"""

from __future__ import annotations

import sys

from core_evm import main


if __name__ == "__main__":
    sys.argv.insert(1, "core-to-evm")
    main()
