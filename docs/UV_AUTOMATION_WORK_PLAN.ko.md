# Low-poly 자동 UV 개선 작업 계획서

- 작성일: 2026-09-12
- 코드 검토 기준: `49024e2c33d856eca53e671916d4a65a5742cb04`
- 상태: 구현 전 계획. 이 문서 추가는 엔진 구현 또는 품질 검증 완료를 의미하지 않는다.
- 대응 Gate: [Acceptance Gate](UV_AUTOMATION_ACCEPTANCE_GATES.ko.md)
- 제품 목표: Windows Electron 앱에서 high-poly → 승인된 low-poly → 자동 seam/UV → 사용자 리뷰 → 승인 결과 내보내기.

## 1. 범위와 기준

low-poly 생성 로직은 유지하고 UV 실행 경로, 왜곡 평가, seam 선택, 결과 판정과 리뷰를 개선한다. 기존 chart 엔진을 재사용한다. 새 UV 엔진이나 LLM의 edge 직접 선택은 범위 밖이다.

사용자 규칙:

1. 인접한 두 면의 법선 사이 각도가 90도 이상이면 반드시 seam을 만든다.
2. 90도 미만은 최대한 유지한다. 펼치기, 겹침 해소, 허용 왜곡 만족에 필요한 절개는 허용한다.
3. 허용 왜곡을 만족하는 결과 중 island 수를 줄인다. island 수와 seam 길이는 따로 평가한다.

각도는 평면 0도, 직각 90도로 정의한다. 이는 low-poly의 기하 법선 기준이며 shading normal 기준이 아니다. 90도 수치 오차 허용값은 Gate G1에서 정의한다. 저폴리 근사 때문에 생긴 급격한 edge도 현재 규칙에는 포함되므로, 리뷰 화면에서 필수 seam과 보조 seam을 구분한다. 사용자 보호와 필수 seam 충돌은 묵살하지 않고 기록한다.

## 2. 코드에서 확인한 출발점

| 위치 | 확인 내용 | 이번 작업 |
|---|---|---|
| `chart_uv_agent/pipeline.py::run_chart_uv` | no-spec 경로에 필수 seam, 실제 UV audit, worst-island 분할, 개선량 기반 revert 존재 | 재사용하고 모드별 중복 루프 축소 |
| `worker/generate_uv_from_seams.py` | seam spec 또는 기존 UV 경계를 가져와 user-spec 경로 실행; 둘 다 없으면 needs_input | 자동 생성 경로 추가 |
| `app/shared/contracts/uvGenerate.ts`, `worker/app_uv_generate_contract.py` | 기본 strict 설정은 자동 추가/수리/필수 seam 강제 모두 false | 명시적 모드 계약 도입 |
| `evaluate_seam_integrity` | strict 플래그 활성화와 seam 추가 자체를 위반 처리 | 모드별 판정 분리 |
| `evaluate_layout_quality` | 겹침과 범위를 확인하지만 왜곡 임계값은 직접 요구하지 않음 | 자동 모드의 엔진 Gate와 최종 상태 연결 |
| `evaluation.py::per_face_stretch` | 전체 스케일 정규화 후 면적비의 절댓값 로그 | 방향별 변형 지표 추가 |
| `pipeline.py::_distortion_report` | checker 이름으로 면적 기반 stretch 집계 | 기존 의미 유지, 새 지표 별도 필드 |
| `segmentation.py::split_chart` | 법선 기반 두 영역 분할; 일반 분할에 선호/노출/경로 길이 입력 없음 | 복수 후보 생성과 제약 연결 |
| `pipeline.py::_run_user_seam_uv` | 선택적 자동 분할에 no-spec 경로와 같은 개선량 revert 없음 | 공통 후보 평가·복원 사용 |
| Electron UV Generate | checker 비교와 후보/지표 UI 존재 | 기존 화면 확장 |

참고 문서: [기존 Seam Core 계획](RULE_BASED_UV_SEAM_CORE_PLAN.ko.md), [보호 영역 실패 기록](IMPORTANT_REGION_UV_POLICY_RESULTS.md), [후속 경로 개선 기록](REGION_AWARE_FACE_UV_RECOVERY_RESULTS.md).

기존 문서의 Blender 실험 수치는 재현 전 참고 자료다. 같은 모델 파일/해시/설정이 확보되기 전 새 Gate 통과 근거로 사용하지 않는다. 본 계획은 이번 자동 UV 확장 범위의 기준이며 기존 strict 모드의 seam 보존 계약은 계속 유지한다.

## 3. 실행 모드와 결과 계약

### auto_generate

- 신규 프로젝트에서 명시적으로 선택 가능한 자동 UV 생성 모드.
- UV/seam spec 없이 승인된 low-poly에서 실행 가능.
- 기존 no-spec chart 경로를 기반으로 필수 seam → 초기 절개 → unwrap → 평가 → 후보 개선 실행.
- 사용자 지정 seam은 lock으로 유지하고 보호/선호 edge는 공통 정책 입력으로 전달한다.
- strict seam spec을 그대로 전달해 user 경로로 우회하지 않는다.
- 사용자가 지정한 모드를 프로젝트·job·report에 저장한다.

### preserve_existing

- 기존 저장 프로젝트에서 mode가 없으면 이 모드로 해석하여 동작 호환성을 유지한다.
- 기존 seam spec 우선, 없으면 유효한 기존 UV 경계를 사용한다.
- 기존 seam의 추가/삭제 없이 unwrap·배치만 최적화한다.
- 원본 seam 집합의 동일성을 edge ID 집합으로 검사한다. 개수만 비교하지 않는다.
- 원본이 90도 규칙을 위반하면 보고하되 몰래 절개하지 않는다. 이 모드의 accepted는 자동 UV 규칙 통과와 구분해 표시한다.

모드와 모순되는 raw 플래그 조합은 요청 단계에서 거절한다. UI/TS/Python의 기본값과 검증을 맞춘다. UI에서 옵션 몇 개를 true로 바꾸는 방식만으로 구현하지 않는다.

### 상태

- `needs_input`: 파일/선택/필수 입력 부족.
- `failed`: 실행 예외, 지원되지 않는 Blender 기능, 유효한 평가를 만들 수 없는 입력.
- `needs_user_review`: 유효한 결과는 있지만 해당 모드의 필수 Gate 실패, 제약 충돌 미해결 또는 예산 소진.
- `accepted`: 해당 모드의 모든 필수 Gate 통과. 아티스트 승인과는 별도 상태.

기존 승인 산출물 교체에는 accepted, 필수 파일 저장 성공, 파일/메시 일치 검증이 모두 필요하다. 저장 실패·취소·worker 종료는 이전 승인 파일을 유지한다. 수치 누락/NaN/Infinity를 0으로 처리해 accepted를 만들지 않는다.

## 4. 왜곡 지표 v2

기존 `stretch_score`와 `checker_distortion_score`의 면적 기반 의미는 유지한다. `metric_version`을 추가하고 방향 왜곡을 별도로 기록한다.

각 삼각형을 직교 로컬 2D 기저로 옮긴 후 3D 표면→UV 변환 J의 특잇값 s1 ≥ s2를 계산한다.

- `anisotropy_ratio = s1 / s2`: 1이면 방향별 배율 동일. 가로 4배/세로 1/4배는 16.
- 기존 면적 왜곡: 전역 배율 정규화 후 |log(UV 면적 / 3D 면적)|.
- 각도 왜곡: 기존 지표를 유지하며 진단에 사용.
- orientation, UV 퇴화, triangle overlap은 왜곡과 별도 correctness 검사.

집계는 3D 표면적 가중으로 global/island별 mean, p95, max, 기준 초과 면적 비율을 제공한다. p95는 면적 누적분포의 95% 지점으로 정의한다. 얼굴처럼 작은 영역이 평균에 묻히지 않도록 island 및 사용자 중요 영역별 결과도 제공한다.

측정은 seam 후보 비교 시 packing 전 공통 배율 조건으로 실시하고, 최종 packing·저장·재읽기 후 다시 실시한다. 후보별 island 재스케일이 면적 개선으로 오인되지 않게 평가 단계와 scale policy를 기록한다. 실제 UV 연결성에서 island를 산출하고 seam flood chart와 다르면 둘 다 기록한다.

오목한 n-gon은 단순 fan으로 평가하지 않고 Blender의 일관된 삼각분할과 loop 대응을 사용한다. 퇴화 삼각형을 조용히 제외하지 않는다. 입력 결함은 진단/실패, 유효 입력에서 발생한 UV 퇴화는 correctness 실패로 분류한다.

## 5. 절개 후보와 공통 제약

기존 `split_chart`를 baseline 후보로 유지하고 별도 후보 공급자를 추가한다.

- 법선 기반 두 영역 분할.
- 왜곡 영역에서 기존 경계까지의 짧은 연결 절개.
- 선호 영역/덜 보이는 면을 경유하는 경로.
- 동일 seam에서 unwrap/relax만 개선하는 무절개 후보.

후보 수·시간·iteration·island cap은 명시적 예산이며 report에 실제 사용량과 종료 이유를 남긴다. 무작위성이 있으면 seed를 저장한다.

필수 seam → 사용자 lock/보호 충돌 처리 → correctness → 왜곡 → island 증가 → 보조 seam 길이/노출 비용 순으로 판단한다. 필수 seam은 제거하지 않는다. 필수 seam과 보호가 충돌하면 필수 seam을 유지하면서 conflict를 표시하고 사용자가 확인하기 전 승인 산출물로 보내지 않는다. 선호는 soft cost다.

제약은 초기 segmentation, 왜곡 분할, 겹침 repair, welded-fold 보조 절개, merge/prune 전체에 전달한다. 분할 뒤 금지 여부만 검사하는 것으로 완료 처리하지 않는다.

각 후보의 seam·UV·관련 상태를 snapshot한다. 임시 적용 → unwrap → 평가 → 채택/완전 복원으로 처리한다. no-spec와 사용자 보조 자동화에 같은 평가 코어를 사용한다.

허용 왜곡 만족 후보가 있으면 필수 조건을 유지하는 후보 중 island 수, 보조 seam 총길이, 노출 비용 순으로 선택한다. 만족 후보가 없으면 실패한 대상 지표의 상대 개선량 15% 이상이며 다른 필수 조건을 악화시키지 않는 후보만 중간 단계로 채택한다. 다른 왜곡 지표의 허용 회귀 예산도 config에 명시한다. 15%는 현행 동작을 잇는 공학적 초기값이며 아티스트 품질 기준은 아니다.

더 이상 개선되지 않으면 실패 위치와 이유를 남기고 needs_user_review로 종료한다. packing 효율이나 보기 좋은 island 외형만을 위해 절개하지 않는다.

## 6. 구현 마일스톤

| 단계 | 수정 대상 | 산출물 | 종료 Gate |
|---|---|---|---|
| M0 기준선 | fixture manifest, 기존 테스트/실행 결과 | 모델 해시·설정·버전·기준 UV·실패 사례 | G0 |
| M1 모드 분리 | TS 계약, Python 계약, IPC/main, generate worker, UI | auto/preserve 명시적 경로와 모드별 상태 판정 | G2, G6 계약 테스트 |
| M2 평가 | evaluation, chart gate, pipeline, UV review | v2 지표와 지표 단위 테스트·최종 UV audit | G1, G3 |
| M3 절개 개선 | segmentation, seam_policy/refinement, pipeline | 후보 비교, 공통 제약, snapshot/revert, 이유 report | G4, G5 |
| M4 제품 연결 | UV Generate/Review, seam editor 계약, export handoff | 모드 표시, heatmap, 이유·제약 충돌·전후 비교 | G6, G7 |
| M5 검증 | 실모델 benchmark, Windows packaged E2E | 고정 profile, 실행 증거, 리뷰어 판단, 회귀 비교 | G8, G9 |

M1은 경로 연결 검증 완료를 뜻하며 자동 UV 제품 출시 승인은 아니다. M2에서 지표가 확정되기 전에 왜곡 임계값을 조정해 품질 통과를 주장하지 않는다.

## 7. 테스트와 리뷰 산출물

기존 seam core, region, user seam, generate integrity 테스트를 확장한다. 신규 테스트 이름은 구현 시 정하되 Gate ID를 테스트와 결과에 연결한다.

필수 fixture: 평면, 큐브, capped cylinder, 구, torus, 완만한 유기체, bevel hard-surface, 퇴화/비정상 입력, 보호 경로가 있는 모델, 실제 승인/거절 UV 쌍.

각 실행에 남길 항목:

- commit, OS, Blender/Python/app 버전, 모델 SHA-256, 메시 fingerprint, mode, seed, config.
- 기준/최종 seam·UV snapshot과 run별 JSON.
- 각 seam의 생성 단계/reason, 필수/사용자/보조 분류, 충돌.
- 후보별 변경 edge, before/after 지표, 채택/거절 이유와 시간.
- checker·heatmap·UV layout·seam overlay와 최종 export 재읽기 audit.
- 기준 결과 대비 island 수, 보조 seam 정규화 길이, 중요 영역 절개, 왜곡, 처리 시간.
- Gate별 PASS/FAIL/BLOCKED/NOT_RUN. 누락된 모델/Windows/리뷰어 검증은 통과로 집계하지 않는다.

앱은 solver 통과와 사용자의 최종 승인을 별도로 표시한다. reviewer feedback은 run/model fingerprint에 묶어 다음 실행에 적용한다.

## 8. 완료 정의

[Acceptance Gate](UV_AUTOMATION_ACCEPTANCE_GATES.ko.md)의 필수 항목을 증거와 함께 통과하고 reviewer가 실제 UV를 승인해야 자동 UV 품질 개선 완료다. 문서 작성, 단위 테스트 통과, 프로세스 exit 0만으로 완료 처리하지 않는다.
