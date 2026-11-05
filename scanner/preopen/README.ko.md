# Pre-Open Technical Scanner V1 — 연구용 WATCHLIST

사용자의 2026-11-05 활성화 요청 범위: 기존 엔진을 보존하고 별도 장전 WATCHLIST를 Telegram으로 전송한다. MC/SCMP, 개장 후 전체 Universe 감시, Entry Planner 실행, Profit Protection 변경은 포함하지 않는다. 후속 단계로 남긴다.

150종목은 config.json에서 관리한다. 기존 Swing 100종목 목록을 복사해 추가 50종목을 설정했으며 Swing 설정은 수정하지 않는다. 종목의 현재 옵션 호가/스프레드를 확인했다는 뜻이 아니다. Alpaca IEX REST만 사용하고 Bigdata, SIP, 유료 데이터, 주문을 호출하지 않는다.

America/Denver 평일 07:10, 07:15, 07:19 수집 및 07:20 최종 발송, 07:21/22 미완료 처리 확인. 서버에서 Alpaca 거래일 달력을 확인하며 휴장일에는 발송하지 않는다. 매일 09:30 ET 개장 10분 전 cutoff를 계산하고 07:25 이후 신규 발송은 차단한다. 07:20 이전 응답만 후보 계산에 사용한다. 보통 마지막 완성봉은 07:15이며 메시지에 표시한다. 정확히 07:20:00 도착을 보장하지 않는다.

## 데이터 정책

장전 native 5분봉과 실제 수신 1분봉의 OHLC·거래량·거래수 정합성을 검사한다. 빈 분을 이전 가격으로 채우지 않는다. 양쪽 자료의 일치가 전체 시장 데이터 완전성이나 당시 누락 0건을 증명하지는 않는다.

초기 설계와 달라진 부분은 희소 1분봉 coverage를 장전 WATCH의 일괄 거절 조건으로 쓰지 않고 SPARSE_RECONCILED로 표시하는 것이다. 기존 strict minute policy의 통과 여부도 비교용으로 남긴다. 기존 DAY/IEX 진입 데이터 기준에는 영향을 주지 않는다. native 5분봉 최소12개, seed63개, 최근3개 native 구간, 신선도330초, IEX 거래대금100만달러 기준은 유지한다. 자료 불충분/조회 실패/기술 탈락을 각각 기록한다.

EMA/VWAP 방향·구조·CHOP·ATR 이격도는 독립 core에서 평가한다. 점수/등급은 검증 전 가설이다. 동시간 20세션 RVOL은 아직 준비되지 않아 UNKNOWN/0점이며 다른 배점으로 보충하지 않는다. Catalyst UNKNOWN은 탈락 사유가 아니다. 목표가가 없으면 R/R UNKNOWN, 위치 점수0이며 실제 진입계획으로 표시하지 않는다.

## 발송/격리

preopen-watch-v1 전용 KV/outbox. 날짜별 watchlist ID로 최대 한 번 발송 시도한다. Telegram 타임아웃은 uncertain_or_failed로 기록하고 자동 재발송하지 않는다. 기존 큐와 엔진 파일은 수정하지 않는다. 데이터 부족으로 0개인 날에도 연구용 결과/상태 메시지를 발송한다.

배포 후 /preopen-watch에서 무료150종목 과거 데이터 probe → Telegram 연결 테스트 → 활성화 순서. probe는 후보 성능 검증이나 Forward Test가 아니며 예약 후보로 재사용하지 않는다. 신규 cron은 /preopen-watch/worker에 JSON secret을 POST하며 기존 cron은 변경하지 않는다. 활성화에는 만료일을 두지 않고 중지 버튼을 제공한다.

## 검증 범위

기존 baseline 3d1525f 기준. 신규 core/runtime/route 테스트, 불완전·미래 봉 제외, 수신시각 cutoff, 희소 봉 정합성, bull/bear 대칭, 휴장일, 중복/인증, 기존 outbox 격리를 검증한다. 과거8종목은 가정한 수신시각의 민감도 비교이며 실시간 성과가 아니다. 최초 실제 예약 실행, 후보 전달 및10거래일 Forward 검증은 활성화 이후 별도 확인해야 한다.

기존 tests/test_v136_live_runtime.py의 entry_exit_persistence_dedup과 gap_then_recovery는 변경 전에도 실패한다. 이번 독립 장전 모듈에서 기존 DAY 로직이나 해당 테스트 기대값을 변경하지 않는다.
