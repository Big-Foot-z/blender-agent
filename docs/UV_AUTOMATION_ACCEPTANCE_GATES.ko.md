# 자동 UV Acceptance Gate

- 대응 문서: [작업 계획서](UV_AUTOMATION_WORK_PLAN.ko.md)
- 기준 코드: `49024e2c33d856eca53e671916d4a65a5742cb04`
- 상태: 구현 전 정의. 현재 모든 신규 Gate는 NOT_RUN.
- 판정: PASS / FAIL / BLOCKED(입력·환경·리뷰어 부재) / NOT_RUN.
- 출시 기준: 필수 Gate 모두 PASS. BLOCKED/NOT_RUN은 PASS가 아니다.

## G0 — 재현 가능한 기준선

**필수.** baseline/new가 동일 low-poly, triangulation, 옵션, Blender 버전과 evaluator를 사용한다.

- 모델 SHA-256, vertex/face/loop fingerprint, 코드 SHA, 설정, seed, 실행 환경 저장.
- 승인된 low-poly의 vertex 좌표·topology·재질 할당을 UV 작업 전후 비교하여 변경 0.
- 기존 실험 문서의 수치를 새 실행 결과로 복사하지 않는다.
- human statue 원본이나 기존 UV가 없으면 해당 실모델 Gate는 BLOCKED.
- baseline도 v2 evaluator로 재측정하여 구 지표와 새 지표를 직접 비교하지 않는다.

증거: fixture manifest, baseline/new report, mesh identity diff.

## G1 — 필수 seam과 최종 UV correctness

**auto_generate 필수. preserve_existing에서는 원본 90도 규칙 위반은 보고하되 원본을 변경하지 않는다.**

| 항목 | 통과 조건 |
|---|---|
| 각도 경계 | 89.9도 비필수, 90도/90.1도 필수; epsilon=1e-5도 이내만 90도로 snap |
| 필수 seam 집합 | mandatory_90_missing = 0 |
| 실제 UV 분리 | mandatory_90_uv_unsplit = 0; 저장·export 재읽기 후에도 0 |
| 뒤집힘/퇴화 | 새로 생성된 UV의 퇴화 triangle 0, 예상 orientation 대비 국소 뒤집힘 0 |
| UV 범위 | 유한값이며 [0,1] ± 1e-4 이내 |
| 겹침 | 의도적 stacking 미지원인 이번 범위에서 양의 면적 triangle 교차 0(수치 허용 오차 제외) |
| 수치 허용 | 정규화 UV tile 면적 1 기준 겹침 총면적 ≤ 1e-8; 경계 접촉 제외 |
| packing 간격 | texture_size에 대한 margin_px 충족; 둘 다 profile에 기록 |
| topology/입력 | 비정상 입력을 명시적으로 진단; 평가 불능을 accepted로 처리하지 않음 |

raster_overlap ≤ 0.005는 기존 coarse screening 참고값이다. 이것만으로 겹침 없음으로 판정하지 않는다. broad phase 후 삼각형 교차 면적으로 확인하며 맞닿은 경계와 겹침을 구분한다. 미러 변환/island 전체 반사와 국소 fold를 구분하는 orientation 정책을 테스트한다. artifact 재읽기에서 edge ID가 바뀌면 fingerprint/대응표를 통해 같은 기하 edge를 검사한다.

증거: cube/각도 경계 fixture, 국소 fold와 교차 fixture, 최종 파일 재읽기 audit.

## G2 — 실행 모드와 호환성

**필수.**

- UV/spec 없는 유효 low-poly + auto_generate → 자동 solver 호출; seam 입력 부족으로 needs_input 처리하지 않음.
- preserve_existing → 원본 seam edge 집합과 최종 집합이 동일(대칭차 0).
- preserve_existing에서 UV/spec 모두 없으면 needs_input.
- 기존 mode 없는 프로젝트는 preserve_existing으로 해석.
- UI → IPC → worker → report까지 같은 mode와 설정 유지.
- 자동 모드는 no-spec 자동 코어를 사용하며 strict integrity 위반으로 오판되지 않음.
- 모순 플래그 조합은 실행 전에 명시적 오류.
- 사용자가 모드를 변경하기 전 기존 프로젝트를 자동 절개로 전환하지 않음.

증거: TS/Python contract 테스트 및 실제 Blender에서 두 모드 실행 기록.

## G3 — 왜곡 지표의 정확성

**필수. 지표 테스트 허용 오차는 아래 값을 사용한다.**

| fixture/변환 | 기대값 |
|---|---|
| 평면 등각·등배율 | anisotropy_ratio = 1 ± 1e-6 |
| 균일 UV scale/rotation/translation | anisotropy 변화 ≤ 1e-6 |
| 가로×4, 세로×0.25 | anisotropy_ratio = 16 ± 1e-6; 전역 정규화 면적 왜곡 ≈ 0 |
| area-preserving shear | 면적 왜곡 ≈ 0, anisotropy > 1 |
| 국소 UV collapse | 평가 불능/퇴화 실패; 0점 통과 금지 |
| tiny bad patch | global 평균과 별도로 max·해당 island/region 값과 초과면적 표시 |
| 오목 n-gon | 실제 triangulation/loop 대응 사용, phantom triangle 없음 |

mean/p95는 3D 면적 가중으로 reference 계산과 일치해야 한다. 후속 global packing의 균일 scale은 정규화 지표를 바꾸지 않아야 한다. 기존 JSON 필드 의미를 유지하며 metric_version 없이는 v1/v2를 섞지 않는다.

증거: 분석적 fixture 단위 테스트, 독립 reference 계산과 오차표.

## G4 — 절개 경로와 제약

**필수.**

- mandatory seam 유지, 사용자 seam lock 제거 0.
- mandatory와 충돌하지 않는 protected edge 절개 0.
- 충돌 시 mandatory 유지 + conflict reason + 사용자 확인 전 needs_user_review.
- 초기 segmentation/왜곡 refinement/overlap repair/welded-fold repair/merge/prune 각각에 동일 제약을 검증.
- 선호 경로 fixture에서 품질·island 수가 동등한 두 후보 중 지정된 preferred 경로 선택.
- region/front_axis 입력이 없으면 visibility neutral; 얼굴 방향을 임의로 추측하지 않음.
- 한 round에서 왜곡 개선 대상 island 하나만 선택. correctness repair는 별도 이유/이력으로 구분.
- 기존 법선 분할과 최소 하나의 다른 경로 후보를 비교하며 선택 이유 기록.
- 동등 seam에서 unwrap-only 후보가 품질을 만족하면 불필요한 추가 절개를 선택하지 않음.

증거: 보호 영역 우회, 경로 없는 보호 영역, 필수 seam 충돌 fixture 및 모든 호출 경로 테스트.

## G5 — 후보 채택·복원과 종료

**필수.**

- 추가 절개는 대상 실패 지표 개선량 ≥ 15% 또는 모든 필수 품질 조건 통과 시에만 채택.
- 다른 필수 correctness/제약을 악화시키는 후보는 무조건 거절.
- 다른 왜곡 지표의 회귀는 고정 profile의 명시적 예산 이내. 예산 누락이면 0.
- 거절/예외/취소 후 seam 집합, UV 좌표, active UV layer 등 후보 변경 상태 완전 복원.
- 동일 입력·버전·seed의 3회 실행에서 seam 집합 동일, float 지표 차이 ≤ 1e-6.
- max_iterations/max_candidates/time_budget/island_cap 도달 시 종료 이유 및 best result 저장; 품질 미달이면 needs_user_review.
- packing/convexity 단독 문제로 추가 절개 0.
- no-spec와 사용자 보조 자동 경로가 같은 채택/revert 규칙을 사용.

증거: 개선 없음/악화/예외 후보 주입 테스트, snapshot 비교, 예산 종료 테스트.

## G6 — 상태와 승인 파일

**필수.**

| 상황 | 기대 동작 |
|---|---|
| 자동 모드 왜곡/필수 seam/correctness 실패 | needs_user_review, 기존 승인 파일 유지 |
| 필수 지표 누락/NaN/Infinity | failed 또는 needs_user_review, accepted 금지 |
| 필수 파일 저장·handoff 실패 | 최종 승인 성공 표시 금지, 이전 파일 유지 |
| 정상 결과 + 필수 Gate 통과 | accepted; 아티스트 승인과 별도 표시 |
| process exit 0 + Gate 실패 | 성공으로 오인하지 않음 |
| cancel/worker crash | 이전 승인 결과 유지, 실행 중 상태 해제 |
| preserve_existing 성공 | seam 유지 성공 표시; 자동 규칙 통과로 표시하지 않음 |

모든 필수 artifact를 임시 경로에 저장·검증 후 승인 포인터를 원자적으로 교체한다. QA/리뷰용 실패 결과는 run 디렉터리에 보존할 수 있지만 export 기본 대상으로 선택하지 않는다.

증거: 상태 분류 unit/integration 테스트, 저장 실패와 중간 종료 주입, 승인 파일 hash 비교.

## G7 — 리뷰와 사용자 피드백

**필수.**

- 3D seam overlay에서 mandatory/user/topology/overlap/distortion 이유 구분.
- UV/3D checker와 v2 heatmap이 동일 최종 UV를 표시.
- global/island별 왜곡·island 수·보조 seam 길이·기준 초과 부위를 확인 가능.
- 추가 seam 클릭 시 대상, 개선량, 생성 이유를 확인 가능.
- 보호/선호/lock을 저장하고 동일 fingerprint 모델에서 재실행 시 적용.
- 모델 topology 변경으로 edge 대응이 무효해지면 제약을 조용히 재사용하지 않음.
- solver accepted와 artist approved가 별도이며 reviewer 거절 이유/run ID 저장.

증거: UI walkthrough, 실제 worker report 연결, 피드백 저장/재적용 E2E.

## G8 — 실모델 품질과 임계값 고정

**출시 필수. synthetic Gate와 구분한다.**

최소 데이터: hard-surface, organic, elongated/cylindrical 각 1개 이상의 low-poly와 리뷰어가 만든 승인/거절 UV 쌍. calibration과 holdout 모델을 분리한다. holdout에는 calibration에 사용하지 않은 최소 1개 모델을 포함한다.

### 두 단계 threshold

1. 지표 구현 검증: G3의 분석적 수치로 판정.
2. 제품 품질 판정: calibration 결과와 reviewer 판단으로 versioned quality profile 확정.

profile 필수 키: metric_version, global/island anisotropy p95 cap, anisotropy max cap, 초과면적 기준 및 cap, area stretch global/island cap, margin_px/texture_size, 회귀 예산, iteration/candidate/time/island 예산.

기존 area stretch 0.50/0.60을 anisotropy 기준에 복사하지 않는다. calibration 전 자동 품질 출시는 BLOCKED. 임계값을 낮추거나 올린 경우 변경 이유/데이터/리뷰어 판단을 기록하고 holdout을 새로 검증한다. 개별 실패 모델을 통과시키려는 사후 완화는 금지한다.

### 비교 통과 조건

- 모든 holdout에서 G1/G4/G6 및 고정 quality profile 통과.
- 원래 profile을 통과한 baseline 대비 island 수 증가 0; 증가는 baseline이 품질 미달이고 새 결과가 통과할 때만 허용.
- 품질이 동등한 후보 중 보조 seam 길이가 최소인 것을 선택한 비교 기록 존재.
- 불필요한 seam을 지적받은 지정 사례 최소 1개에서 같은 품질 조건으로 중요 영역 보조 seam 길이 감소 및 reviewer 승인.
- edge 개수뿐 아니라 동일 mesh 기준 seam 실제 길이와 모델 bounding-box diagonal로 정규화한 길이 보고.
- reviewer가 checker, 실제 절개 위치, island 구성 검토 후 승인. 수치 통과만으로 대체하지 않음.
- 모든 모델 결과 공개: 실패 사례를 표에서 제외하지 않음.

증거: frozen profile, fixture manifest, baseline/new 표, artifact, reviewer verdict. 성능은 고정 Windows 하드웨어에서 3회 측정해 median/최대 시간과 peak memory를 보고한다. 목표 시간 예산은 calibration 시 고정하며 초과 시 출시 BLOCKED.

## G9 — Windows 패키지와 전체 회귀

**출시 필수.**

- 고정한 지원 Blender 버전과 Windows x64에서 packaged 앱으로 실행.
- 한글·공백 경로, Blender 경로 탐지/명시 지정, 지원되지 않는 버전 오류 검증.
- high-poly → 기존 low-poly 승인 → UV 없는 상태의 자동 생성 → 리뷰 → 최종 export 재읽기 성공.
- preserve_existing 동작 및 이전 프로젝트 열기 회귀 없음.
- 취소/오류/재실행과 승인 파일 보호 검증.
- 기존 seam editor/UV review/low-poly 파이프라인 관련 테스트 통과.

검증 명령(실행 위치 명시):

```bash
# 저장소 root
python -m pytest

# app 디렉터리
npm ci
npm run typecheck
npm run test:integration
npm run build

# Windows 빌드 환경의 app 디렉터리
npm run dist:win
```

테스트 skip은 이유와 영향 Gate를 기록한다. 빌드 성공은 Windows 실사용 E2E 통과가 아니다. Blender 기반 테스트/packaged E2E는 실제 환경의 증거가 별도로 필요하다.

## 실행 결과 작성 양식

| Gate | 상태 | 코드/설정 버전 | 증거 경로 | 실패 이유/다음 조치 |
|---|---|---|---|---|
| G0–G9 각각 | NOT_RUN | 미정 | 미정 | 구현 전 |

각 Gate를 독립 행으로 작성한다. 구현 완료와 실행 검증, solver accepted와 아티스트 승인을 구분한다. 이 문서 자체에는 실행하지 않은 PASS를 기입하지 않는다.
