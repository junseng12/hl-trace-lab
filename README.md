# HL Trace Lab

Hyperliquid의 `userFills`, 실시간 `trades` WebSocket, `recentTrades`를 비교해 **체결별 상대방을 어디까지 복원할 수 있는지** 재현하는 연구용 프로젝트입니다.

## 빠른 시작 — Windows Git Bash

VS Code에서 Git Bash 터미널을 열고:

```bash
bash 실행파일/설치.sh
source .venv/Scripts/activate
cp .env.example .env.local
chmod 600 .env.local
```

Linux/WSL에서는 `source .venv/bin/activate`를 사용합니다.

`.env.local`에 Testnet Master 공개 주소와 Agent 정보를 입력한 뒤 먼저 읽기 전용 점검을 실행합니다.

```bash
python 실행파일/테스트넷_사전점검.py --env .env.local
```

`.env.local`의 `HL_ENABLE_SIGNED_ACTIONS=false`는 변경하지 마세요. Testnet 실행기가 네트워크·주소·Agent 키·한도를 검증한 뒤, 해당 실행 프로세스의 메모리에서만 서명을 일시 허용합니다.

실험 순서:

1. `테스트넷_T1_단건체결.py`
2. `테스트넷_T2_T3_분할체결.py --phase t2`
3. `테스트넷_T2_T3_분할체결.py --phase t3`
4. `체결정보_보존성_재조회.py`

정확한 명령과 결과 해석 방법은 [실험 실행 가이드](문서/실험_실행_가이드.md)를 보세요.

## 폴더 구조

```text
문서/       실험 방법·해석 가이드
실행파일/   수집·사전점검·Testnet 실험 스크립트
테스트/     오프라인 단위 테스트
결과/       로컬 실행 산출물(Git 제외)
```

## 보안·실행 범위

- `.env.local`, 개인키, 시드 문구, 실행 산출물은 Git에 올리지 않습니다.
- 자동 주문 코드는 Hyperliquid **Testnet만** 허용합니다.
- Mainnet에서는 `공개자료_수집.py`로 수집하고, UI에서 정상 소액 거래만 수동 수행합니다.
