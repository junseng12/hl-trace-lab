# 01. 전체 자산 이동 8경로 추적

이 디렉터리는 보고서 **2.2 전체 자산 이동 8경로**를 공개 자료만으로 다시 조회하기 위한 코드와 결과 예시를 모은다. 모든 추적기는 읽기 전용이며 거래 생성, 서명, 전송을 수행하지 않는다.

## 공통 원칙

- 개인키, 시드 문구, Agent 개인키는 입력하지 않는다.
- 공개 주소, TX hash, Core action hash, 대략적인 시각과 수량만 사용한다.
- 서로 다른 장부의 hash가 다르면 억지로 연결하지 않고 주소, 자산, 수량, 시각과 프로토콜 식별자를 결합한다.
- 후보가 하나일 때만 유일한 연결로 판정하고, 후보가 없거나 둘 이상이면 미확정으로 남긴다.
- `NOT_FOUND`는 거래가 없다는 증명이 아니라 조회 범위 또는 자료가 부족하다는 뜻이다.

### 보수적 판정값

| 판정 | 의미 |
|---|---|
| `DIRECT_IDENTIFIER_MATCH` | 동일 hash, 동일 tid, 공통 프로토콜 ID 또는 브리지 `transactionId`가 양쪽 기록을 직접 연결함 |
| `UNIQUE_COMPOSITE_MATCH` | 주소, 자산, 수량, 시각 조건을 모두 만족하는 후보가 정확히 하나임 |
| `AMBIGUOUS` | 조건을 만족하는 후보가 둘 이상이므로 개별 건을 확정할 수 없음 |
| `NOT_FOUND` | 설정한 범위에서 후보를 찾지 못함 |
| `INSUFFICIENT_DATA` | 필요한 RPC, receipt, bridge status 또는 원장 기록이 부족함 |

위 명칭은 보고서에서 사용하는 통합 판정 용어다. 실제 JSON의 `classification`은 경로별 구현에 따라 `UNIQUE_MATCH`, `EXACT_PROTOCOL_ID_MATCH`, `AMBIGUOUS_OR_NOT_FOUND`, `MISMATCH` 등으로 출력될 수 있다. 따라서 문자열만 보지 말고 `candidateCount`, 실제 원문, 시간 차이와 제한 조건을 함께 확인한다.

각 결과 JSON은 입력값, 실제 조회 URL, 원자료, 후보 수, 시간 차이, 최종 판정과 한계를 함께 보존해야 한다.

## 경로 1. 외부 네트워크 → HyperCore

**목적:** Arbitrum 입금 TX가 HyperCore deposit으로 반영됐는지 확인한다.

- 공개 입력: Arbitrum TX hash, Hyperliquid 사용자 주소, 토큰, 수량
- 조회 자료: Arbitrum TX·receipt·USDC Transfer, HyperCore deposit 기록
- 산출물: 외부 TX와 Core deposit의 hash, 주소, 수량, 시각 비교 JSON
- 판정: 양쪽에 같은 hash가 있고 주소·자산·수량도 일치할 때 `DIRECT_IDENTIFIER_MATCH`

```bash
python 01_자산이동_8경로/route01_external_to_core.py \
  --network mainnet \
  --external-tx-hash 0x1dfe9ba61c7898cdf159314cbee4664eacea344430eee832520208ad4a293518 \
  --user 0x012b198d1c4b85e5ac34ebdc0a1eac911931ef2c \
  --amount 9.8 \
  --output 결과/route01.json
```

검증 예시:

- Arbitrum TX 및 Core deposit hash: `0x1dfe9ba61c7898cdf159314cbee4664eacea344430eee832520208ad4a293518`
- 자산: `9.8 USDC`

한계: Arbitrum USDC 한 사례를 검증한 결과이며, 모든 입금 체인과 토큰을 대표하지 않는다.

## 경로 2. HyperCore → 외부 네트워크

**목적:** Core withdrawal action과 외부 체인의 실제 수신 TX를 연결한다.

- 공개 입력: Core withdrawal hash, 외부 수신 TX hash, 사용자·수신 주소
- 조회 자료: HyperCore withdrawal, Arbitrum TX·receipt·USDC Transfer
- 산출물: 출금액, 수신액, 수수료 차이, 주소와 시각 비교 JSON
- 판정: 명시된 수수료 차이를 반영한 뒤 유일한 외부 수신 후보가 확인되면 `UNIQUE_COMPOSITE_MATCH`

```bash
python 01_자산이동_8경로/route02_core_to_external.py \
  --network mainnet \
  --user 0xdd7b27b8c8160f3fd37f9f52b5d1ebd4d67c9db9 \
  --destination 0x012b198d1c4b85e5ac34ebdc0a1eac911931ef2c \
  --core-hash 0x92c74a471ca5735794410447c4b54e02012c002cb7a89229368ff599dba94d42 \
  --external-tx-hash 0xfb029e3b96b951191e88e47f546ffacc5c0de232cfedce0c4653c6903ee27ff2 \
  --requested-amount 10 \
  --output 결과/route02.json
```

검증 예시:

- Core 출금: `10 USDC`
- Arbitrum 도착: `9.8 USDC`
- 관측 차이: `0.2 USDC`

한계: 실험 수신 주소가 Hyperliquid 이메일 지갑의 Arbitrum 주소였고, 수신 자산은 이후 다시 HyperCore로 입금됐다. 독립 외부 지갑에 장기 보관한 종단 실험은 아니다.

## 경로 3. HyperCore W1 → HyperCore W2

**목적:** HyperCore 내부 `sendAsset`을 송신·수신 주소와 잔고 변화로 확인한다.

- 공개 입력: Core action hash 또는 송신 주소, 수신 주소, 토큰, 수량, 시간 범위
- 조회 자료: 양쪽 주소의 Core 원장과 잔고
- 산출물: `sendAsset` 원문, W1 감소, W2 증가, EVM TX 부재 확인 JSON
- 판정: action의 user·destination·token·amount가 일치하면 `DIRECT_IDENTIFIER_MATCH`

```bash
python 01_자산이동_8경로/route03_core_to_core.py \
  --network testnet \
  --core-hash 0x0672d372f95cf04607ec042c963c49010800eb5894500f18aa3b7ec5b850ca30 \
  --sender 0xdd7b27b8c8160f3fd37f9f52b5d1ebd4d67c9db9 \
  --recipient 0x012b198d1c4b85e5ac34ebdc0a1eac911931ef2c \
  --token HYPE \
  --amount 0.001 \
  --start-time-ms 1792951300000 \
  --end-time-ms 1792951500000 \
  --output 결과/route03.json
```

검증 예시: Testnet `0.001 HYPE`, Core hash `0x0672d372…50ca30`, 일반 EVM TX 없음.

한계: Unified Account에서는 기존 `spotSend`가 거부될 수 있으므로 원장에는 일반화된 `sendAsset` 형식이 남는다. 이 실험은 Testnet HYPE 한 사례다.

## 경로 4. HyperCore → 동일 주소 HyperEVM

**목적:** Core action과 별도로 생성되는 프로토콜 System TX를 연결한다.

- 공개 입력: 사용자 주소, Core hash 또는 시간 범위, HYPE 수량
- 조회 자료: Core `send`, HYPE System Address, `eth_getSystemTxsByBlockNumber`, 표준 RPC 비교
- 산출물: Core action, System TX 후보, 후보 수, 표준 RPC의 `null` 결과
- 판정: System Address 방향, 사용자, 정확한 수량과 인접 시각을 만족하는 후보가 하나면 `UNIQUE_COMPOSITE_MATCH`

```bash
python 01_자산이동_8경로/route04_core_to_hyperevm.py \
  --network mainnet \
  --core-hash 0x0a0df0f00c4ff7590b87044662b02b02072500d5a743162badd69c42cb43d143 \
  --user 0xdd7b27b8c8160f3fd37f9f52b5d1ebd4d67c9db9 \
  --start-time-ms 1791793100000 \
  --end-time-ms 1791793350000 \
  --amount 0.22 \
  --output 결과/route04.json
```

검증 예시:

- Mainnet Core action: `0x0a0df0f0…43d143`
- Mainnet System TX: `0x5ba4bfcf…0f58d8`
- Testnet Core action/System TX: `0xe90183d6…604e7` / `0x4edeb345…5edfe`

한계: Core action hash와 System TX hash는 다르다. System TX는 표준 TX·receipt 조회에서 누락될 수 있으므로 전용 RPC가 필수다. 현재 검증 코드는 HYPE System Address를 기준으로 하며 다른 토큰 주소를 임의로 재사용하면 안 된다.

## 경로 5. HyperEVM → 동일 주소 HyperCore

**목적:** 사용자가 서명한 EVM TX와 별도의 Core credit을 연결한다.

- 공개 입력: EVM TX hash, 예상 사용자 주소와 시간 허용 범위
- 조회 자료: EVM TX·receipt·Received event, Core `spotTransfer` credit
- 산출물: EVM TX, event, Core 후보, 후보 수와 시간 차이
- 판정: 주소, System Address, 토큰, 정확한 수량, 시각을 만족하는 Core credit가 하나면 `UNIQUE_COMPOSITE_MATCH`

```bash
python 01_자산이동_8경로/route05_hyperevm_to_core.py \
  --network mainnet \
  --evm-tx-hash 0xaa850cad3585218cfb4ab487128bf36f495ef0bb889b479b1869f0c97f84dd39 \
  --output 결과/route05.json
```

검증 예시:

- Mainnet EVM TX/Core credit: `0xaa850cad…84dd39` / `0x36f4d97f…afb78b`, `0.20 HYPE`
- Testnet EVM TX/Core credit: `0x5c71f441…9ceef` / `0xc8d9b638…9ed27`, `0.001 HYPE`

한계: EVM TX와 Core credit hash는 다르다. 반복 전송이 같은 시간창에 있으면 후보가 충돌할 수 있다.

## 경로 6. HyperEVM W1 → HyperEVM W2

**목적:** HyperEVM 내부의 일반 EVM value transfer를 표준 RPC로 확인한다.

- 공개 입력: EVM TX hash, 예상 송신·수신 주소
- 조회 자료: `eth_getTransactionByHash`, receipt, block timestamp, 수신 잔고
- 산출물: from·to·value·receipt status와 잔고 변화 JSON
- 판정: 성공 receipt와 from·to·value가 일치하면 `DIRECT_IDENTIFIER_MATCH`

```bash
python 01_자산이동_8경로/route06_hyperevm_to_hyperevm.py \
  --network testnet \
  --tx-hash 0x351dde2cc74d87f1aafa1128a07271a984e7253b8fed8392b6ce91460dee0426 \
  --expected-from 0xdd7b27b8c8160f3fd37f9f52b5d1ebd4d67c9db9 \
  --expected-to 0x012b198d1c4b85e5ac34ebdc0a1eac911931ef2c \
  --expected-amount 0.002 \
  --output 결과/route06.json
```

검증 예시: Testnet `0.002 HYPE`, receipt status `0x1`, Core 기록 없음.

한계: Testnet Explorer가 불안정할 수 있어 공식 RPC 응답을 원본으로 보존해야 한다. Token Transfer는 native value transfer와 별도 로그 해석이 필요하다.

## 경로 7. 외부 네트워크 → HyperEVM

**목적:** Bitcoin 입력 TX와 변환 후 HyperEVM 수신 TX를 공개 프로토콜 식별자로 연결한다.

- 공개 입력: Bitcoin TXID, HyperEVM 수신 TX hash
- 조회 자료: Bitcoin vin·vout·OP_RETURN, HyperEVM TX input·receipt, LI.FI 연결 기록
- 산출물: 양쪽 TX와 공통 식별자, 입출력 자산, 시각 차이
- 판정: 양쪽 원문에서 같은 프로토콜 식별자가 확인되면 `DIRECT_IDENTIFIER_MATCH`

```bash
python 01_자산이동_8경로/route07_external_to_hyperevm.py \
  --network mainnet \
  --bitcoin-txid 687d2f96caf8c507ea15b684373a6ad0db0f646b95321a2902ad72e14f9d94f7 \
  --hyperevm-tx-hash 0x6be0aab1ba261a01407a78fa800db64567d87ee58d0d7c437645af0c897f5c65 \
  --protocol-id 0x74f9c9f78e05c163dfb8a903396316f1ed7279acce9b8527b220e7bb77863e3a \
  --output 결과/route07.json
```

검증 예시:

- `0.00101690 BTC → 0.892642462386395710 HYPE`
- 공통 식별자: `0x74f9c9f78e05c163dfb8a903396316f1ed7279acce9b8527b220e7bb77863e3a`
- 관측 지연: 약 36초

한계: OP_RETURN·calldata 구조는 사용된 브리지와 라우터에 종속된다. 임의의 BTC→EVM 서비스에 그대로 적용할 수 없다.

## 경로 8. HyperEVM → 외부 네트워크

**목적:** HyperEVM 출발 TX와 외부 체인 도착 TX를 LI.FI status의 `transactionId`로 연결한다.

- 공개 입력: HyperEVM 출발 TX hash, 출발·도착 chain ID
- 조회 자료: 양쪽 EVM TX·receipt, LI.FI `/v1/status`
- 산출물: `transactionId`, tool, status/substatus, sending·receiving TX, 주소, 자산, 수량, 시각
- 판정: 공식 상태 응답이 양쪽 hash를 포함하고 `DONE/COMPLETED`이면 `DIRECT_IDENTIFIER_MATCH`

```bash
python 01_자산이동_8경로/route08_hyperevm_to_external.py \
  --network mainnet \
  --source-tx-hash 0xd78168d3b3da5580049913b5ebc7a364c92c254298bff390b576005d6e23865d \
  --to-chain 8453 \
  --expected-destination-hash 0x9d520e4d85fb95e0c448cfbe6467d998cb84d4b819ca72da6a603919591cb8b4 \
  --output 결과/route08_base.json

python 01_자산이동_8경로/route08_hyperevm_to_external.py \
  --network mainnet \
  --source-tx-hash 0xbda87663e89b807dbbc28f2da0fec377fbadc3de39e9b0d969b61eb5253f360c \
  --to-chain 1 \
  --expected-destination-hash 0x6ad163e57c9aab32e86fed1d5c26e2d5aa3a6417b4f657bc6bd9fbdbeb4f1c4d \
  --output 결과/route08_ethereum.json
```

검증 예시:

| 경로 | 출발 TX | 도착 TX | LI.FI `transactionId` | 도구·상태 |
|---|---|---|---|---|
| HyperEVM→Base, 15 USDC | `0xd78168d3…23865d` | `0x9d520e4d…cb8b4` | `0xd72c3266474d9246e79f4798d79b7a1fd6e3eee384a8b97da066485746cb74d0` | `polymerStandard`, `DONE/COMPLETED` |
| HyperEVM→Ethereum, 9.767115 USDC→0.00431227 ETH | `0xbda87663…f360c` | `0x6ad163e5…f1c4d` | `0xcc52fabc797a6c8555e8352156d932374f62e1b06a2abd19a001e9fcd62cd536` | `layerswap`, `DONE/COMPLETED` |

한계: status API 보존 기간과 도구별 인덱싱 상태에 영향을 받는다. 조사 시점의 응답을 즉시 JSON으로 보존해야 하며, 내부 정리용 `TX0217` 같은 ID를 LI.FI `transactionId`로 사용하면 안 된다.

## 비밀값 관리

이 추적 코드는 공개 자료만 사용하므로 실제 개인키가 필요하지 않다. 추가 실험 실행용 개인키가 필요한 별도 스크립트가 있더라도 다음 원칙을 적용한다.

- GitHub에는 빈 `.env.example`만 올린다.
- 실제 값은 저장소 밖 또는 Git에서 제외된 `.env.local`에 사용자가 직접 입력한다.
- 결과 JSON, 로그, 오류 메시지에 개인키·시드 문구·인증 헤더를 기록하지 않는다.
