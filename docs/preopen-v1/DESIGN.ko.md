# 설계 원본 및 검증 이력

2026-11-05 활성화 구현 범위와 데이터 정책 변경은 scanner/preopen/README.ko.md를 우선 참조한다. 아래는 최초 설계와 검증 보충 이력을 보존한 것이다. 전체 장중150종목 감시와 Entry/Exit 연동은 미구현이다.

# Market Forge Pre-Open Technical Scanner V1 — 구현 설계안

작성: 2026-11-04. 상태: 설계 및 코드 검토 완료, 신규 스캐너 미구현·미배포. 기존 알림, 엔진, 구독, 일정은 이번 작업에서 변경하지 않았다. 임계값은 모두 검증 전 연구 가설이다.

## 결정 제안

기존 V13.16은 차트 비교 기준으로 보존하고, 서버에 독립적인 **장전 후보 선정 모듈**을 추가한다. 첫 단계의 출력은 매매 신호가 아닌 WATCHLIST다. 다음 단계에서만 개장 후 후보 추적을 연결한다. 기존 DAY/IEX 경로에 새 필터를 삽입하지 않는다.

무료로 시작할 수 있지만, **150종목 조회 가능성과 150종목의 충분한 장전 데이터 확보는 다르다.** IEX의 거래량을 전체 시장 거래량으로 표시하지 않는다. 데이터가 부족하면 후보 0건과 DATA_INSUFFICIENT를 구분한다. AAPL이 7:20에 선정됐을 것이라고 현재 가격 사례만으로 결론 내리지 않는다.

## A. 현재 코드에서 확인한 역할과 재사용 범위

GitHub 기본 브랜치 파일을 다시 읽었다. 주요 blob은 engine.py `4d5402c5224467b1b42832eb64b1ad7198a78476`, runtime.py `ebbe9aca308645c7d94a2a9f6f9fd8fa370022b6`, free_iex.py `fe18ed612de6c1ec56527ddeedfb38b1c9cc9e7b`, alpaca.py `84eb91430a179d58b9009025477e0e16de01a853`이다. 앞의 세 소스를 이 문서의 sources/에 보관했다. V13.16은 기존 로컬 원본을 별도로 보존하고 해시를 기록했다. 이는 현재 TradingView 설치본과 바이트 동일함까지 확인했다는 뜻은 아니다.

| 구성 | 현재 역할 | 재사용 / 별도 작업 |
|---|---|---|
| V13.16 Pine | 차트의 MC/SCMP·CTN·종료와 live exit 상태 관리 | 원본 유지. 같은 feed/session/adjustment에서 비교. Python 엔진과 동등하다고 가정하지 않음 |
| DAY TEST V13.39 | BATS 20종목 확정 1분봉을 서버에서 1·2·3·5·10분으로 집계 | 기존 비교군으로 고정. 새 장전 데이터는 이 엔진에 그대로 넣지 않음 |
| 무료 IEX | SPY·QQQ·NVDA·TSLA·AMD, REST 매분 조회, 1분+2분 FAST | 인증·calendar·발송 식별 방식 참고. 5종목 고정/RTH 전용 검증/매번 종일 재생 구조를 150종목에 복사하지 않음 |
| scanner/alpaca.py | feed 명시, calendar, pagination, baseline 캐시 | 공통 읽기 기능 활용. 현재 최대100종목 제한과 기본 sip 설정을 신규 모듈에 그대로 상속하지 않음 |
| store/outbox | 인증 후 기록, 큐, 중복 억제, 전송 | 별도 prefix와 메시지 유형 사용. 기존 큐를 비우거나 기존 포지션을 수정하지 않음 |
| Replay 도구 | A/B/C/D, 고정 ATR, MFE/MAE, 후보 차단 비교 | 장전 as-of snapshot과 후보 선정 여부를 입력에 추가 |

현재 DAY 진입은 단순 EMA 방향 일치가 아니다. 같은 3분 주기 안에서 각 시간봉이 body/ATR(1.25·1.05·0.90), 상대거래량(1.50·1.35·1.25), 종가 위치, EMA/VWAP, 4봉 돌파, EMA9 이격 조건을 통과해야 한다. **3분마다 돌파를 재증명하게 하는 구조와 장전 후보 선정을 연결하는 것만으로 지연이 해결되지는 않는다.**

현재 데이터 객체는 고정20종목·정규장·양수 거래량을 검사한다. 장전150종목을 위해 그 검사를 풀면 운영에 영향을 준다. 별도 ExtendedHoursBar 계약을 만든다.

### 현재 종료 로직의 정확한 특징

- CTN: 진입 후 5분·10분 추세 정렬 시 1회. 수익이 양수일 필요가 없다.
- TRIM: MFE가 현재 직전1분 ATR의1.75배 이상, 약화 확인, 반납이 `max(0.85 ATR, MFE의35%)` 이상일 때 경고. 수량·청산비율·실현손익 장부가 없다.
- PROTECT_X: 같은 수익 활성 조건과 약화 확인 후 반납이 `max(1.45 ATR, MFE의60%)` 이상일 때 종료.
- FAST_X: 큰 반대봉 또는 두 반대봉에 구조 이탈·EMA 약화가 겹칠 때 종료. 기준가격 아래로 갔다고 즉시 작동하는 고정 손절이 아니다.
- 현재 우선순위는 PROTECT_X → FAST_X → TRIM. ATR은 진입 때 고정되지 않고 매번 달라진다. MFE는 이전 완성봉까지의 최고 유리 가격으로 계산해 같은 봉의 고저 순서를 임의 가정하지 않는다.
- V13.16에는 별도로 일반2봉/모멘텀3봉 최소 보유와4봉 cooldown 및 예외 경로가 있다. 5분 차트에서는 각각10/15분·20분 규모지만 모든 live exit에 동일하게 적용된다고 단정할 수 없다. 원본 전체 상태와 별도로 비교해야 한다.

## B. 150종목 수집 설계와 무료 데이터 한계

### 기본 구성

`Universe 설정 → IEX REST 수집 → 시점별 원본 보관 → 완성5분봉 특징 → 데이터/CHOP/CHASE 검사 → 방향별 순위 → WATCHLIST outbox`

1. Universe는 총150종목까지의 설정 파일로 관리한다. SPY/QQQ도150개에 포함한다. 기존100종목을 검토해 시작하고, 사용자가 언급한 대형주와 검증 가능한 유동성 기준으로 확장한다. 옵션 활발 여부는 후보선정 메타데이터이며 실제 옵션 호가 통과를 의미하지 않는다.
2. Catalyst 외부종목은 발행시각·최초수신시각이 컷오프 이전인 경우에만 제한된 교체 슬롯에 추가한다. 유료 뉴스가 없는 Phase1에서는 수동 지정 또는 이미 보유한 적법한 자료만 사용하고 UNKNOWN을 허용한다.
3. REST는 25~50종목 묶음, 최대3개 동시 요청부터 시작한다. 서버 전체 rate budget을 공유하며 기존 IEX 작업과 합산해 제한한다. 공식200회/분을 모두 쓰지 않고 초기100회/분 이하로 제한한다. 429는 백오프하고 실패 종목만 격리한다.
4. `next_page_token`을 끝까지 확인한다. 첫 페이지가 일부 종목만 포함해도 나머지를 후보0건으로 처리하지 않는다. 모든 종목은 SUCCESS / INSUFFICIENT / REQUEST_FAILED 중 하나로 마무리한다.
5. 이전 세션 지표 초기화와 과거 동시간 거래량 baseline은 미리 캐시한다. 7:20에는 증분 조회와 계산만 수행한다. 현재 IEX처럼 매분 모든 과거 봉을 다시 내려받고 재생하지 않는다.
6. Phase1은 분봉 또는 공급자5분봉의 연구 비교로 구현할 수 있다. 실시간150개 WebSocket 구독을 전제로 하지 않는다. **개장 후에도 전체150종목의 발견용 스캔을 유지한다.** 07:20 Top10은 우선 WATCHLIST이며 감시 대상을 제한하는 목록이 아니다. 전체 Universe는 증분 REST 분봉 수집으로 감시하고, 우선 후보는 별도 정밀 추적한다. 무료 streaming 제한은 정밀 추적에만 적용하며 나머지 종목을 감시에서 빼지 않는다. 실제 관측 간격과 누락률은 두 경로를 따로 보고한다.

### 중요한 제한

Alpaca Basic은 실시간 IEX, WebSocket30종목, historical API200회/분이다. 실시간 전체 거래소는 유료 범위다. REST 복수종목 조회와30종목 streaming 제한은 별개다. 무료 SIP를 최신 실시간으로 자동 대체하지 않는다. [Alpaca 요금·권한](https://docs.alpaca.markets/us/docs/about-market-data-api), [복수종목·pagination](https://docs.alpaca.markets/us/reference/stockbars)

IEX 장전은08:00~09:30 ET다. 09:20까지 당일5분봉은 최대16개이며 실제 체결 부족으로 더 적을 수 있다. 04:00부터의 전체 프리마켓 VWAP과 다르다. **IEX_PM_VWAP_0800**으로 명명하고, 공급자 VWAP 또는 분봉 VWAP×volume을 합산한다. 거래 VWAP이 없고 HLC3를 쓰는 경우에는 VWAP_APPROX로 명시해 동일한 지표라고 하지 않는다. [IEX 거래시간](https://www.iex.io/resources/trading/trading-hours-holidays)

EMA는 같은 feed의 과거 완성봉으로 초기화한다. 초기 가설은 최소63개5분봉 seed + 당일 최소12개 유효5분봉이다. 거래가 없던 구간을 가짜 봉으로 만들지 않는다. EMA는 관측된 거래봉 기반임을 기록하며, 시간 간격이 불균일하면 coverage 검사를 통과하기 어렵게 한다. ATR은 세션 내 범위와 overnight gap을 분리한다. IEX에서 seed가 충분해도 당일 구조가 부족하면 WATCH 선정에서 제외한다.

### 7:20 시점의 정의

초 단위로 정확히7:20에 발송하면서7:20에 막 끝난 봉의 수신까지 보장할 수는 없다.

- V1 기본: `selection_cutoff=07:20:00 America/Denver`에 이미 수신된 데이터만 고정한다. 07:20까지 계산된 완성봉 중 `bar_end <= cutoff AND first_received_at <= cutoff`를 만족해야 한다. 마지막 데이터가07:15이면 그대로 표시한다.
- 계산·발송 목표는07:20분대, 초기 작업 deadline07:21. 이는 목표이지 현재 보장된 SLA가 아니다. 지연 시 오래된 순위를 정상 시간의 결과처럼 보내지 않고 DELAYED/INCOMPLETE로 표시한다.
- 07:20봉이07:20:03에 도착했다면 정시 snapshot에는 넣지 않는다. 그것을 쓰는 실험은 별도07:20+settle snapshot으로 분리한다. 과거 재생에서 미래 수신을 소급하지 않는다.
- 과거 API만으로 당시 first_received_at을 모르면 `historical_available_at_unknown`이며 실제07:20 forward 성과와 분리한다.

## C. 후보 선정과 진입의 정확한 초안

아래 수치는 구현·검증을 가능하게 하는 초기값이다. 개별 성공사례에 맞춰 확정한 최적값이 아니다. 모든 값은 config_hash로 고정한다.

### 1. 데이터와 방향 검사

방향 `d=+1/-1`, `A=직전 확정5분봉까지의 ATR14`, `P=최신 확정봉 종가`를 사용한다. A가0/미준비면 INVALID_DATA다.

하드 제외: invalid OHLCV, 역순/충돌, 미완성 집계, seed 부족, 당일 유효5분봉12개 미만, 당일1분 coverage80% 미만, 최근15분의 집계 불완전, 마지막 완성5분봉이 컷오프보다5분30초 이상 오래됨. 초기 IEX 유동성 가설은 당일 해당 feed 거래대금100만달러 이상이다. 이 금액은 전체시장 유동성 보증이 아니며, 통과율과 놓친 기회를 별도로 검사한다.

Bull은 EMA9>EMA21, 두 EMA의3개 완성봉 변화>0, 최근3개 종가 중2개 이상이 당일 PM VWAP 위여야 한다. Bear는 부호 반전으로 완전 대칭이다. 구조는 trailing3봉 block과 이전3봉 block의 고점·저점을 비교한다. 두 block의 고저가 모두 같은 방향으로 이동해야 한다. 미래 확인봉을 요구하는 pivot은 사용하지 않는다.

CHOP 후보: 최근6봉 EMA9/21 방향교차3회 이상, 또는 efficiency<0.25와 평균 wick비율>0.60 동시 충족. efficiency=`abs(C_now-C_6bars_ago)/sum(abs(1bar changes))`; 분모0이면0. 이 gate의 제거 실험도 함께 기록한다.

### 2. 점수 100점: 항목별 근거 저장

| 항목 | 배점 | 점수 규칙 초안 |
|---|---:|---|
| EMA | 30 | 방향 정렬10, EMA9의3봉 기울기 같은 방향10, EMA21의3봉 기울기 같은 방향10 |
| VWAP | 20 | 최근3종가 모두 유리한 쪽10 / 2개면5, VWAP3봉 기울기 같은 방향5, 최근6봉 VWAP교차1회 이하5 |
| 구조 | 20 | trailing block 고점·저점 동방향 각5, 최근3봉 EMA9/21 접근(0.25A 이내) 후 유리한 쪽 종가 회복5, 방향별 무효화 가격이 올바른 쪽이며 구조 유지5 |
| 거래량·유동성 | 15 | 같은feed·동시간의 이전20세션 median 대비 누적RVOL≥1.5이면10 / ≥1이면5, 최근5분 거래량/직전3개5분봉 평균≥1.2이면5 |
| Momentum | 10 | 방향별3봉 변화≥0.3A이면5, efficiency≥0.5이면5 |
| 위치 | 5 | No Chase 통과 및 사전 구조 stop을 이용한 계획의 R/R≥1.5이면5 |

동시간 baseline이 없으면 RVOL 점수는0과 UNKNOWN 사유를 남긴다. 분모를 줄여100점으로 재정규화하지 않는다. 유동성 하드 제외는 점수와 독립이다. 85이상 A,75이상 B,65이상 C. 모든필수검사를 통과한65이상에서 방향별 최대5개. 동점은 데이터 신선도→작은chase→ticker순으로 고정한다. 점수는 성공확률이 아니다.

### 3. No Chase / 계획

`ema_extension=d*(P-EMA9)/A`, `vwap_extension=d*(P-PMVWAP)/A`, `impulse=d*(P-C_3bars_ago)/A`를 기록한다. 첫 실험의 EXTENDED는 각각1.5/2.5/2.0 초과 중 하나다. overnight gap은 별도열이며 이 extension에 합산해 차단하지 않는다.

장전 고저와의 거리는 기록하되 고점에 가깝다는 이유만으로 자동 탈락시키지 않는다. Breakout watch에서는 오히려 핵심 level이다. 거래량 증가 없는 급등은 volume_fail과extended를 함께 기록한다.

READY에서 다음을 동결한다: setup type, breakout/reclaim level, entry zone, chase limit, invalidation/stop, structural target, ATR, planned_at. LONG stop은 최근 눌림 저점-0.15A, SHORT는 반대. 목표가 주변 과거 저항/지지로 정해질 수 없으면 TARGET_UNKNOWN이며 READY 진입계획을 완성하지 않는다. 임의2R 목표만 만들어 R/R을 항상 통과시키지 않는다.

Entry zone 초안: 유효 돌파 level부터 유리한 방향0.25A까지. Chase Limit은 level+방향0.5A. 구조 stop 기준 손실거리와 가장 가까운 알려진 목표까지의 보상거리로 R/R≥1.5 검사. 가격이 Chase Limit 밖이면 WAIT/MISSED로 기록한다. 새 눌림/구조가 확인되면 새 plan_revision으로 재평가하며 과거 후보 시간을 바꾸지 않는다.

상태는 DATA_INVALID(품질), CHOPPY, EXTENDED, CLEAN, READY, INVALIDATED(기술 시나리오 실패)를 구별한다. score가 높아도EXTENDED를 READY로 바꾸지 않는다.

### 4. 개장 후 연결: 같은 돌파를 세 번 요구하지 않는다

- WATCH: 장전 후보. 매수·매도 아님.
- ARMED: 개장 후 새 유효봉으로 기존 계획이 유지되고, 가격이 entry zone 부근. PM VWAP과09:30부터 리셋한 RTH VWAP을 별도 유지한다.
- EARLY_WATCH: 첫 확정1분봉에서 방향 EMA/VWAP, 계획 level 돌파/reclaim, volume·위치 검사 통과. 1분 확인의 초기 volume·위치 가설은 동시간 RVOL≥1.2, 방향 종가위치≥0.65, body/직전1분ATR≥0.5다. 봉 자체가 미확정이면 INTRABAR_PREVIEW로만 기록한다.
- PROVISIONAL_MC/SCMP: 완성2분봉에서 같은 방향을 유지하고 level 실패가 없으며 body/직전2분ATR≥0.5, 동시간RVOL≥1.2, 방향 종가위치≥0.65, No Chase·R/R 통과. 3분 확정 전임을 명시한다. Phase2 기본은 Shadow만 기록한다.
- CONFIRMED_MC/SCMP: 완성3분봉에서 같은 episode의level 유지, EMA9/21·RTH VWAP 방향 일치, 초기2분의 반전 없음, volume·No Chase·R/R 재검사. 3분봉도 새로운4봉 고점을 다시 돌파해야 한다는 조건은 추가하지 않는다.
- 1분 후보의 유효기간은 최초 후보부터180초. 다음 cycle로 이전 PREP를 무기한 넘기지 않는다. 방향 반전·stop/구조 실패는INVALIDATED. 재정렬에는 새episode 필요.
- 3분만 뒤늦게 통과하고 가격이멀어진 경우CONFIRMED 진입을 강제로 만들지 않고MISSED. 이미PROVISIONAL을 발송한 실험은 확인 알림을 새매수로 보내지 않는다.
- 시간봉은09:30ET에 정렬한 고정 window. 첫1분완료09:31,2분09:32,3분09:33. 그 이후 겹치는2/3분 window는 실제begin/end와age를검사한다. 신호를 받은시각으로봉을만들지 않는다.
- 최신 완성5분봉은 context로 사용한다. 두EMA기울기·VWAP·구조가 모두 강한반대이면 block, 약한반대/중립은 risk tag. 첫5분 전에는preopen context임을표시한다. 5분MC 자체는기다리지않는다. 이gate 유무도동일후보비교한다.

후보 ID는 `date+ticker+feed+origin+source_bar_start+direction+config_hash`로 구분한다. origin은 PREOPEN 또는 INTRADAY_DISCOVERY다. 장중 후보의 최초 감지 시각을 07:20으로 소급하지 않는다. 진입 ID는 후보 ID+setup_revision+episode다. Preview는 기존 V13.16/Day/IEX의 상태를 수정하지 않는다.

장중 새 Momentum·돌파·반전 후보는 데이터 품질, 구조, 거래량, No Chase 검사를 통과하면 WATCHLIST로 승격한다. 승격은 진입이나 Telegram 발송을 의미하지 않는다. 이후 동일한 ARMED→EARLY_WATCH→PROVISIONAL→CONFIRMED 검증을 거친다. Top10 밖이라는 이유로 차단하지 않는다. 반전은 새로운 방향·episode로 기록하고, 이전 방향 후보를 덮어쓰지 않는다. 보유 포지션은 순위 하락이나 목록 교체 때문에 관리 대상에서 제거하지 않는다.

## D. Profit Protection 개선 가설과 11/4 사례

### 변경 전 검증할 설계

1. `ATR0`, `R0=abs(entry-stop)`, initial_stop을진입당시고정한다. 현재ATR·현재추세는별도보조값이다. 변동성이커졌다고보호선이저절로멀어지지않는다.
2. CTN은 `HOLD/TREND_ONLY`가 기본이고 currentPnL_R, MFE_R, giveback_R를표시한다. 5·10분정렬은진행상태이지추가매수조건이아니다.
3. TRIM 실험: +1R에최초도달한완성봉에서50% 모의청산을기록한다. 실제fill은다음이용가능bid/ask로평가하고없으면다음봉open을proxy로표시한다. 알림가격전량체결을가정하지않는다. 50%는연구가설이며현재엔진의비율이아니다.
4. Profit protection 실험: MFE≥1.5R 이후, 이전완성봉peak에서 `max(0.5R,0.4*MFE)` 반납하고 EMA9 또는 마지막확인눌림구조를깨는경우 잔량PROTECT_X. 같은봉의high를먼저안것처럼trail을당기지않는다.
5. FAST_X: 고정구조stop이확정1분close로깨지거나, 두완성1분봉의구조붕괴+EMA/VWAP회복실패. 실제quote를받는별도실험만 stop근처의짧은유지확인을검토한다. OHLCV만으로초단위손절을구현했다고하지않는다.
6. 우선순위는 DATA_HALT(신규판단중지·보유위험미확인 알림) / 유효데이터일때 FAST_X → PROTECT_X → TRIM → CTN. EOD close는세션마감정책으로별도처리한다. 같은cycle full exit가발생하면TRIM/CTN은발송하지않고정리된reason만보존한다.
7. 동일진입에서현재exit vs 구조stop만 vs 고정ATR보호 vs TRIM+보호를비교한다. exit후5·10분회복도측정해너무이른청산의기회손실을보여준다. 분봉내stop/target둘다닿으면순서불명으로표시하거나보수적시나리오를함께보고한다.

### 사용자가 제공한 가격으로 확인 가능한 범위

아래는가격산술이며11/4 원시시계열재생결과가아니다. 메시지시각·분봉·MFE·ATR·발송지연이없으면chase나최적exit을확정할수없다.

| 종목 | 계산 | 해석 |
|---|---|---|
| JPM | TRIM +0.79, 최종 +0.51, TRIM후반납0.28 | 50%를350.16,나머지를349.88에체결했다고가정하면주당+0.65. 실제비율·체결·MFE미확인. TRIM후0.28반납만으로실패판정불가 |
| MSFT | 555.05−557.83=−2.78, 약−0.498% | CTN도−0.33인상태. 현재CTN이수익신호가아님을보여줌. 진입당시EMA/VWAP/ATR/이전가격이없어추격여부는미확정 |
| AAPL | 353.78−354.09=−0.31, 약−0.088% | 7:20선정여부는그때의장전데이터로재생해야함. 현IEX5종목수집에AAPL이없으므로기존IEXsnapshot만으로복원불가 |
| META | CTN +0.59, 최종−1.20, 약−0.187% | 관측된CTN가격부터1.79반납. 진짜최고가격은더높았을수있음. TRIM/보호발동이가능했는지는시계열과R/ATR필요 |

검증작업은네종목의실제신호시각·가격·feed를원본과연결하고,그날07:20snapshot→개장후매봉판단→관리이벤트를순서대로재생한다. BATS와IEX 결과를한성과표의동일가격으로섞지않는다. 데이터가사후수정됐으면별도재구성자료다.

## E. 안정성·알림·일정·준비상태

### 누락/중복

RDDT·AXP의정확한11/4누락횟수는이번설계검토에서조회하지않았다. 현재runtime은 gap의시작시각마다새DATA_GAP ID를만들므로, 한장애중여러빈구간이생기면알림이반복될수있다. 같은ID재전송과다른gap의반복통지는다르다.

새모듈은종목/feed별 `HEALTHY→DEGRADED→RECOVERING→HEALTHY` incident를저장한다. 최초1회알림,같은incident중반복은로그만,복구1회알림. 초기30분digest는운영오류용별도채널/메시지에묶는다. 새완성봉이정상이고필요한집계window가모두완전해진뒤에만신규진입재개. 누락가격은채우지않는다. DATA_HALT는포지션종료나가격유지를뜻하지않는다.

원본과revision을append-only로보관하고과거전송결정을재작성하지않는다. 공급원간확인에는동일시각·동일adjustment·신선한가격만쓴다. 비교feed가없으면CROSS_FEED_UNVERIFIED이며PASS로표시하지않는다. 두신선한feed의차이가초기가설 `max(0.25% 가격,0.3 ATR5)`보다크면해당종목신규진입중지. 단일IEX는제한된연구모드로허용하고실전옵션체결가능판정은하지않는다.

bar_start/end,first_received_at,revision_received_at,decision_at,enqueue_at,send_started/completed_at,Telegram message_id를분리한다. 클라이언트도착시각은별도사용자확인이없으면UNKNOWN이다. 기준시각은UTC저장·MT/ET변환. stale entry를복구후재발송하지않고missed로기록한다. outbox ambiguous timeout은delivery_unknown으로남겨무조건재전송하지않는다. Telegram API를포함한완전한exactly-once를보장한다고하지않는다.

### 메시지

`PRE-OPEN V1 · RESEARCH · WATCH ONLY · 실제 진입 아님`

일자 / selection cutoff MT·ET / 실제발송시각 / feed IEX / data-as-of / 평가성공종목수·불충분·실패수를공통헤더에표시한다. 방향별최대5개,종목별가격·score항목·grade·EMA9/21·VWAP종류·구조·volume·momentum·chase·setup·invalidation·catalyst UNKNOWN·candidate_id를넣는다. 예시점수나가격을실제값으로채우지않는다. 길면방향별분할하고각part ID로중복을막는다.

### 일정

America/Denver 07:20 새작업만추가한다. 거래일calendar에서해당일open/close를읽고,정규open−10분과일치하는지검사한다. 휴장일은거래후보발송없음. 조기폐장에는entry cutoff와EOD관리도실제close기준이다. 기존07:00/08:00/13:00작업은수정하지않는다. 이전조회에서08:00/13:00작업까지정상실행을확인한것은아니므로배포전읽기전용으로기존일정과실제전송을대조한다. 13:00고정브리핑은조기폐장일에는장후정보일수있고새진입을허용하지않는다.

### 준비된 것 / 추가로 필요한 것

| 항목 | 준비 | 남은 확인 |
|---|---|---|
| TradingView Essential | 기존차트/알림유지. 공식20technical alerts | 새스캐너는TV alert0개사용. 현설치본·세션·보정조건맞추기 |
| Alpaca무료IEX | 기존5종목인증·조회·Telegram동작증거 | 150종목장전coverage, ratebudget, 실제계정권한/응답,seed품질 |
| Vercel+DB+cron | 기존API/저장/스케줄인프라 | 3개분할작업의deadline·동시부하·수신지연·비용계측 |
| Bigdata | 기존연결/분석자료 | 현재잔여credit·서버API권한은이번에재확인하지않음. Phase1외부호출0. 캐시/배포권한확인후Phase4 |
| 옵션호가 | 검증된실제체결데이터없음 | spread/거래비용실측없이는옵션성과·실전유동성PASS판정불가 |

Essential 공식20technical alerts와서버REST확장은별개여서새스캐너때문에TV업그레이드할필요는없다. [TradingView](https://www.tradingview.com/pricing/)

Vercel은현재WebSocket지원안내가있지만최대함수수명에따라끊기므로종일수집기메모리를믿으면안된다. Phase1은REST와영속checkpoint로진행한다. Phase2stream을선택할때만재연결·복구·수명·계정비용을측정한다. [Vercel](https://vercel.com/kb/guide/do-vercel-serverless-functions-support-websocket-connections)

유료SIP가필요해지는판단기준은무료feed의장전coverage부족,시장전체volume/NBBO필요,동시stream확대다. 지금구매하거나자동fallback하지않는다. Bigdata비용은`후보수×refresh횟수×실측요청당credit`으로예산을잡고승인받는다. 잔액이나단가를추정하지않는다. 개인연구권한과유료구독자대상파생신호/데이터이용권한도분리확인한다.

## F. 구현 파일·순서·통과기준

### 파일 계획 — 신규 namespace `preopen-v1:`

| 파일(제안) | 역할 |
|---|---|
| config/preopen_v1/universe.json | ticker/feed/기준시점/옵션상태/그룹,150개상한,중복검사 |
| config/preopen_v1/rules.json | score/quality/chase/entry/exit실험값과version |
| scanner/preopen_v1/data.py | 별도extended-hours모델,분할REST,pagination,as-of,revision |
| scanner/preopen_v1/features.py | EMA/ATR/두종류VWAP/구조/volume/CHOP |
| scanner/preopen_v1/rank.py | hard reject,점수근거,상태,top5+5 |
| scanner/preopen_v1/planner.py | level/entry zone/chase/stop/target/RR 동결 |
| scanner/preopen_v1/runtime.py | 거래일실행·snapshot·lease·부분실패·복구 |
| scanner/preopen_v1/messages.py | WATCHLIST만발송,보안·길이·중복 |
| scanner/preopen_v1/entry_shadow.py | Phase2 1/2/3분episode·확정비교 |
| scanner/preopen_v1/risk_shadow.py | Phase3 별도잔량장부·종료가설 |
| scanner/preopen_v1/health.py | 종목별incident·알림억제 |
| tools/preopen_replay.py / tests/test_preopen_*.py | prefix replay·성과·데이터검사 |

현재app의등록지점에새router를추가하는최소수정만한다. 배포entrypoint는구현전원격파일에서확인한다. 운영engine.py/runtime.py/free_iex.py와Pine원본은변경대상에서제외한다. 공통helper추출이필요하면기존callers의결과동일성회귀검사와별도commit을선행한다. 신규실험enable/sender킬스위치는기존DAY/IEX/Swing과독립이다.

### 순서

1. **보존·데이터시험:** V13.16원본·설정·운영소스해시보관. AAPL포함대표5~10개장전IEXcoverage를먼저측정한다. 이단계실패를점수낮추기로해결하지않는다.
2. **Phase1 오프라인:** Universe100→150설정,고정컷오프원본,features/rank/planner,선정과탈락표. 후보가0이어도정상결과를구별한다.
3. **Phase1 Forward:** 설정을 고정하고 최소10거래일 장전 선정 및 전체 Universe의 장중 승격을 Shadow로 기록한다. 신규 Telegram 메시지는 발송하지 않는다. 데이터 부족으로 정상 평가되지 못한 거래일은 정상 검증일과 구분한다.
4. **Phase2:** 같은후보에서기존3분진입 / 2분provisional / 3분structure confirmation 비교. 5분context추가효과별도. 기존운영교체없음.
5. **Phase3:** 진입을고정한exit실험. TRIM수량장부와잔량손익추가. entry와exit개선을동시에바꿔효과를혼동하지않는다.
6. **실제 Telegram 검증:** 후보 선정·Early Entry·동일 진입 Exit 비교가 끝난 뒤 연구용 메시지의 실제 수신, 발송 지연, 중복, 만료 처리를 검증한다. 이 단계까지 완료하기 전 신규 경로를 운영으로 전환하지 않는다.
7. **Catalyst 선택 통합:** 기술 검증 후 별도 진행한다. 뉴스가 없거나 UNKNOWN이어도 기술 조건을 통과한 후보는 유지한다. 크레딧·권한 확인 전 15분 전종목 재검색은 없다.

## 추가 반영 사항 — 전체 감시와 구현 경계

이 절은 2026-11-04 추가 요청을 반영한다. 단계의 우선순위는 **기존 누락 원인 및 복구 확인 → 장전 후보 선정 Shadow → Early Entry 비교 → Profit Protection 비교 → 실제 Telegram 검증**이다. Catalyst 연결은 이 과정의 필수 조건이 아니다.

### 전체150종목 감시의 실행 계약

- 각 분마다 모든 활성 Universe 종목에 대해 마지막 수신 봉, 마지막 평가 시각, 성공/부족/실패 상태를 기록한다. 25~50개씩 나누어 동시성 제한 아래 조회하고, 지연된 그룹도 다음 cycle에서 누락 없이 이어간다.
- 150종목×1분봉 한 개의 정상 증분은 분당150개 행이다. HTTP 요청은 종목별150회가 아니라 묶음 요청과 pagination으로 처리한다. 이전 구간 복구·seed·다른 엔진 요청까지 공유 rate budget에 포함한다.
- 초기 목표는 각 종목에 매분 새 완성봉 평가 기회를 주는 것이다. 이는 초단위 실시간 보장이 아니다. 실제 cadence P50/P90/P95, 최신 봉 age, cycle completion, 지연된 종목 수를 공개한다. 무료 데이터/서버로 달성하지 못하면 감시 범위를 몰래 줄이지 않고 제약을 보고한다.
- REST 발견 경로는 항상 전체 Universe를 유지한다. 정밀 추적 슬롯이 꽉 차면 새 후보는 WATCH/QUEUED_PRIORITY로 남기고 매분 평가한다. 슬롯 배정 여부와 무관하게 보유 포지션의 종료 감시는 유지한다. 동적 구독 교체·권한·연결 제한은 별도 부하 테스트 대상이다.
- 장중 승격도 `first_detected_at`, `promoted_at`, `origin`, `reason_codes`, `feed`, `data_age`, `plan_revision`을 저장한다. 기존 후보 재발견은 동일 episode로 합치며, 새로운 구조만 새 episode를 만든다.
- 후보 발견, 우선순위 상승, 진입 검증을 서로 다른 이벤트로 보관한다. 2분·3분에서는 최초 level 유지와 구조를 검사하며 매번 새로운 고점/저점 돌파를 요구하지 않는다. 3분 미확정 상태를 CONFIRMED로 표시하지 않는다.

### IEX 품질 선행 검증

첫 데이터 검증에는 AAPL·MSFT·META·AMZN·NVDA·TSLA·SPY·QQQ를 포함한다. 이전 자료의 IEX5종목만 검사해서 주요 대형주가 모두 가능하다고 결론 내리지 않는다.

각 종목에 대해 다음 표를 생성한다:

`ticker | requested | received_1m | complete_5m | coverage | seed_count | latest_bar | first_received_at | data_status | reject_reason | technical_pass`

data_status는 SUFFICIENT / INSUFFICIENT / FETCH_FAILED다. 조회 성공 후 체결 봉이 적은 경우와 API 실패를 분리한다. DATA_INSUFFICIENT 종목의 기술 점수는 미평가이며 0점과 구분한다. 모든 Universe 종목은 반드시 하나의 행을 갖는다. AAPL의 실패 이유가 데이터 부족인지 실제 기술 조건인지 따로 보고한다. 무료 IEX 장전 실측은 아직 수행하지 않았다.

### Entry Planner와 Exit Shadow 분리

Entry Planner는 사전 진입 범위·무효화 가격·목표·R/R을 만든다. Exit Shadow는 동일한 진입 이벤트 목록과 동일한 이용 가능 시세를 입력받아 종료 방식만 비교한다. Exit 결과를 보고 과거 Entry Planner를 변경하지 않는다.

각 exit 경로는 `trim_fraction`, `remaining_fraction`, `realized_R`, `unrealized_R`, `weighted_total_R`, `MFE_R`, `giveback_R`, `execution_assumption`을 기록한다. 예를 들어 50%를 +1R에 청산하고 잔량50%를 -0.2R에 종료하면 합계는 +0.4R이다. TRIM 전량과 최종 종료 전량을 각각 수익에 더하지 않는다. 실제 옵션 체결 데이터가 없으면 기초자산 모의 결과로만 표시한다.

### 기존 엔진의 마지막 확인 상태와 11/2~3 누락 원인

아래는 이전 조사 및 재설정 시점의 증거다. 이 설계 수정에서 새 장중 상태를 다시 조회한 결과는 아니다.

| 경로 | 마지막 확인한 활성/발송 경로 | 남은 확인 |
|---|---|---|
| V13.16 | 로컬 원본 보존. TradingView 차트 엔진이며 DAY/IEX와 별개 | 현재 설치본·개별 alert·Telegram 연결의 실시간 상태는 미확인. 차트 표시를 발송 활성으로 간주하지 않음 |
| DAY V13.39 | TV V136 NEW RESEARCH Bank1·2 → 서버 research139recovery → outbox → Telegram. 두 alert Active. 서버 enabled=true, 11/10 22:01:39 MT까지 재활성화 | 다음 장 실제 이벤트의 eligible→sent 및 만료 경보 확인 |
| IEX FAST | 무료5종목 REST Worker → free-iex-v1 outbox → Telegram. 활성 유지, 매분 cron | 약1분 처리 지연과 당일 데이터 품질. 이 수정에서 cadence 변경 없음 |
| Swing | 1시간봉13 Bank → swing webhook → outbox → Telegram. Bank10 Restart 후13개 모두 Active | Calculation error 원인 자체는 미해결. 재시작 후 새 시간봉 수신 및 실제 PLAN 전달 확인 필요 |
| V13.33~35 | 과거 TV 알림 중지. V13.35 서버 enabled 잔여 설정과 구 Worker가 남아 있음 | 경로 정리는 별도 운영 변경. 새 스캐너에서 재사용·재활성화하지 않음 |

11/2~3에 신호가 적었던 이유는 다음과 같다. 상세 근거는 `../audit-2026-11-02-03/REPORT.ko.md`에 있다.

- 기존 DAY20은 10/31 발송 허용 만료로 11/2 진입6건, 11/3 진입11건을 생성하고도 발송0건이었다. 전체 진입·관리 이벤트는 각각21건/30건이며 비활성19건/29건, stale2건/1건이었다.
- IEX는 11/2 수정 봉 오류 이후 종목별30봉,08:00까지로 멈췄다. 정확한 오류 반복 횟수는 저장되지 않았다. 11/3은 장 끝까지 처리됐고 후보7건 중2분 조건에서6건 탈락, NVDA 진입1건과 관리2건이 sent였다.
- BATS5종목의 11/2 기록에는12:24~12:50 공통27분 공백이 있었다. 11/3에는 각각390봉이었다. IEX 11/3 일부 종목의 빈 분봉은 공급 누락인지 무거래인지 확정되지 않았다.
- Swing Bank10 중단으로8종목이 빠져92종목만 수신됐다. 11/2 DIA PLAN1건,11/3 DIA REJECTED1건은 실제 sent였다. REJECTED는 OPEN_GATE 전략 거절이며 HTTP422가 아니다. 정확한 하위 gate 값은 보존되지 않았다.
- NVDA IEX 조건 충족09:02와 큐 등록09:03:01 사이의 지연이 확인됐다. 새 후보 선정으로 이 전달 지연이 자동 해결됐다고 하지 않는다.

### 파일·PR·테스트·배포 상태

설계 문서의 로컬 절대 경로:

`/Users/marcuscho/Documents/Codex/2026-09-12/referenced-chatgpt-conversation-this-is-an/outputs/preopen-v1-design/DESIGN.ko.md`

| 구분 | 이번 상태 |
|---|---|
| GitHub PR | 이 설계 문서는 PR로 제출하지 않았다. 로컬 산출물이다 |
| 이번 수정 파일 | DESIGN.ko.md. 변경 이력은 CHANGELOG.ko.md에 별도로 기록 |
| 기존 보존 파일 | sources/의 V13.16 원본과 서버 소스3개, source-sha256.json. 이번에 내용 변경 없음 |
| 구현 코드 | 신규 모듈 미구현. 운영 코드 수정 없음 |
| 검사 | 문서 내 Top10 제한·단계 순서 충돌을 수정하고 보존 소스 해시를 확인. 엔진 테스트나150종목 데이터 검증을 수행한 것은 아님 |
| Vercel | 이 설계 작업의 신규 배포 없음. 기존 배포를 변경하지 않았으며 최신 배포 건강 상태를 이번에 재확인하지 않음 |
| Telegram | 이 설계 수정으로 신규 메시지 발송·설정 변경 없음 |

구현 착수 후에는 commit/PR, 변경 파일, 테스트 명령과 결과, 데이터 검증 결과, 배포 SHA/상태, 실제 메시지 수신 여부를 각각 보고한다. 설계 완료를 구현·배포·수신 완료로 표현하지 않는다.

### 테스트와 판정

- 구현검사: bull/bear대칭,시간대/DST/휴장/조기폐장,페이지 누락,불완전봉거절,수정봉/중복/순서역전,1분누락시2·3·5분집계차단,사후뉴스/봉주입에도과거선정불변,receiver outage,중복스케줄,Telegram불명응답,기존엔진결과불변.
- 부하검사: 50→100→150에서요청수·실제완료율·P50/P95수집/계산/전송지연·DB비용. 일부종목실패가전체마감시간을무한연장하지않음.
- Forward10거래일은최소관측기간이지성공보장아님. 상승/하락·추세/횡보·갭장세별표본이부족하면연장한다. 과거튜닝구간과새거래일을분리한다.
- 선정평가: 7:20가격부터+30/+60분방향수익·MFE/MAE,개장후계획stop보다먼저+1R에닿았는지,같은 봉에서 양쪽 가격이 접촉하면 순서 불명(ambiguous). 선정하지않은유효종목의놓친움직임도비교해selection bias를공개한다.
- 진입평가: 동일후보의조건충족/실제발송시각,가격차이,chase/ATR,평균 R·중앙R·평균이익/손실비·최대손실·연속 손실/낙폭,+5/+10,MFE/MAE,즉시 반전 비율,전체candidate 분모.
- 종료평가: 잔량가중실현/미실현R,peakgiveback,exit후회복,잘못된조기종료. 옵션quote가없으면underlying-only와비용민감도만보고한다.
- 운영통과: 설명되지않은미래참조·미완성확정·오래된재발송·중복0건,종목별결측이모두분류됨,실제연구메시지수신확인. 정상데이터에서deadline미달은기술보류.
- 전략통과: 충분한동일후보표본에서진입지연과반납개선이보이고MAE/최대손실/놓친기회가악화되지않는지보고한다. 표본불확실성을표시하고자동운영승격은하지않는다. 단위테스트개수나점수상승을매매성능으로보고하지않는다.

## 이번 산출물의 경계

이번에 완료한 것은 현재 원격 코드 검토, 제공 가격 계산, 공식 데이터 제약 확인, 구현 설계와 소스 보존이다. 11/4 원시봉 재생, 150종목 장전 실측, Bigdata 잔액 확인, 새 Telegram 발송·스케줄 설치, 10일 Forward는 아직 수행하지 않았다. 기존 기능 삭제나 운영 설정 변경은 없다.


## 2026-11-05 검증 보충 — 운영 적용 전 데이터 계약

11/4 무료 IEX 장전 8종목을 실측했다. 기존 엄격 분봉 coverage 기준은 8종목 모두 차단했다. 반면 native 5분봉 82개는 반환된 1분봉의 OHLC·거래량·거래수와 모두 일치했고, 그중79개는 희소 분봉 구간이었다. 따라서 기존의 “1분 누락이면 모든 상위 봉 무효” 가정은 장전 native 5분봉에 일괄 적용하기 전에 검증해야 한다.

기존 기준은 비교 기준으로 보존한다. 별도 Shadow에서는 SPARSE_RECONCILED 상태를 사용하되, 오류 없음·유동성 충분·기술 조건 통과와 동일시하지 않는다. 빈 가격 생성은 금지하며 장중 진입 기준을 자동 완화하지 않는다. 실시간 가용시각은 사후 API로 입증되지 않았다.

상세 결과: ../preopen-quality-2026-11-04/REPORT.ko.md. 로컬 진단만 추가했고 GitHub PR 및 Vercel 배포는 하지 않았다. 150종목 검증과 Forward Test는 미완료다.
