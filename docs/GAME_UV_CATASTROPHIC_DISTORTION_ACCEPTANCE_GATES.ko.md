# 게임용 UV Catastrophic Distortion Acceptance Gates

- 대응 문서: `GAME_UV_CATASTROPHIC_DISTORTION_RECOVERY_PLAN.ko.md`
- 대상: 기존 게임 UV 개선 작업 이후 신규 후속 milestone
- 판정값: `PASS / FAIL / BLOCKED / NOT_RUN`
- 출시 원칙: **필수 Gate가 하나라도 FAIL/BLOCKED/NOT_RUN이면 자동 UV 최종 승인 금지**
- 주의: 아래 수치 중 `engineering default`는 구현 시작값이며, 실모델 calibration 후 frozen profile로 버전 고정한다.

---

## CG0 — 재현 가능한 실패 기준선

**필수.**

현재 관찰된 가시/바늘형 UV distortion 사례를 regression fixture로 고정한다.

통과 조건:

- 입력 mesh SHA/fingerprint 저장
- Blender 버전, 코드 SHA, quality profile id, seed 저장
- seam edge set 저장
- UV hash 저장
- checker / heatmap / layout 이미지 저장
- distortion_v2 / correctness / catastrophic report 저장
- 동일 조건 3회 실행에서 failure region face ids가 동일하거나 명시된 deterministic 대응 규칙과 일치

**FAIL:** 재현할 수 없는 이미지만 있고 수치/UV 상태를 다시 만들 수 없음.

---

## CG1 — Triangle Correctness Hard Gate

**필수. 최우선 Gate.**

| 항목 | PASS 조건 |
|---|---|
| non-finite UV | 0 |
| UV degenerate triangle | 0 |
| local flip triangle | 0 |
| exact positive-area overlap | total <= `1e-8` |
| mandatory 90 seam missing | 0 |
| mandatory 90 UV unsplit | 0 |
| bounds | `[0,1] ± 1e-4` 또는 명시된 tile policy |

이 Gate는 평균/p95 distortion과 무관하다.

한 triangle이라도 local collapse/flip이면 전체 결과 FAIL.

**증거:** `uv_correctness.json`, face ids, exact-overlap samples.

---

## CG2 — Catastrophic Triangle Hard Gate

**필수.**

최종 UV는 catastrophic triangle을 포함할 수 없다.

engineering default:

| metric | PASS 조건 |
|---|---|
| `anisotropy_max` | `<= 8.0` |
| `near_collapse_count` | `0` |
| `max_uv_triangle_aspect` | `<= 40` |
| normalized local area ratio | `[1/25, 25]` 범위 밖 hard-fail triangle `0` |
| invalid metric | `0` |

단, 위 수치는 calibration 대상이며 profile id에 고정한다.

중요 규칙:

- global mean이 낮아도 FAIL 가능
- island p95가 낮아도 FAIL 가능
- bad triangle 면적이 작아도 **hard max를 넘으면 FAIL**
- NaN/Inf를 0으로 치환해 통과시키면 FAIL

**증거:** `uv_catastrophic.json`, worst triangle list, face/loop ids.

---

## CG3 — Catastrophic Region Gate

**필수.**

하나의 bad triangle이 아니라 인접 cluster가 형성되는 경우 별도 판정한다.

PASS 조건:

- `bad_region_count == 0`
- `bad_area_fraction <= frozen_profile.bad_area_fraction_cap`
- boundary spike region `0`
- same-island self-cross 관련 region `0`

engineering default:

```text
bad_area_fraction_cap = 0.005
```

hard max 초과 triangle이 하나라도 있으면 bad-area fraction과 관계없이 CG2에서 FAIL.

**증거:** region face sets + heatmap overlay.

---

## CG4 — Heatmap / Gate Data Identity

**필수.**

사용자 화면과 acceptance evaluator가 같은 UV를 보고 있어야 한다.

PASS 조건:

- heatmap `uv_hash` == final gate `uv_hash`
- checker `uv_hash` == final gate `uv_hash`
- `mesh_fingerprint` 동일
- `metric_version` 동일
- final gate가 사용하는 per-triangle metric과 heatmap source가 동일
- invalid/degenerate triangle이 시각화에서 0/파랑으로 숨겨지지 않음

**FAIL 예:** report PASS인데 화면에는 명백한 빨간 spike가 있고, 조사 결과 preview가 이전 UV를 렌더링함.

---

## CG5 — Repair Ordering

**필수.**

catastrophic failure 발생 시 repair 순서는 다음이어야 한다.

```text
1. same-seam re-unwrap/relax 후보
2. relief seam 후보
3. quality refinement
4. merge-back
```

PASS 조건:

- seam 추가 없이 해결 가능한 fixture에서 seam 증가 0
- packing/convexity 문제만으로 catastrophic repair seam을 만들지 않음
- 한 repair round에서 target bad region 하나만 처리
- correctness repair와 distortion repair의 reason이 history에서 구분됨

---

## CG6 — Relief Seam Candidate Quality

**필수.**

새 seam 후보는 단순히 target 평균을 개선했다고 채택할 수 없다.

채택 조건:

- mandatory seam 유지
- protected/forbidden edge 위반 0
- local flip 증가 0
- overlap 증가 0
- UV degenerate 증가 0
- catastrophic hard-fail triangle 감소
- bad-area fraction 감소 또는 0 유지
- target metric 개선율 >= frozen `min_improvement_ratio`
- 새로운 tiny/sliver island 생성 0
- island cap 미초과
- non-target metric regression budget 통과

위 항목 중 하나라도 실패하면 후보는 reject + full restore.

---

## CG7 — Candidate Rollback Integrity

**필수.**

거절/예외 후보는 흔적을 남기면 안 된다.

PASS 조건:

candidate 전후 reject 시 아래가 완전 동일:

- seam edge set
- UV loop coordinates
- active UV layer
- UV layer name
- object mesh topology
- user seam locks
- material assignment
- accepted baseline `uv_hash`

예외 주입 테스트에서도 동일해야 한다.

float UV 비교 tolerance: `1e-12` 또는 serialization round-trip에 맞춘 고정 tolerance.

---

## CG8 — Tiny / Sliver Island Gate

**필수. 게임용.**

최종 repair가 UV를 작은 조각으로 폭발시키지 않아야 한다.

profile에 다음 키를 필수로 둔다.

```text
min_island_width_px
min_island_area_px2
max_island_bbox_aspect
max_island_perimeter_area_ratio
max_tiny_island_area_fraction
```

PASS 조건:

- 일반 island의 `min_width_px >= max(2*margin_px+2, 6)` engineering default
- `sliver_island_count == 0`
- tiny island는 명시된 `detail` 예외 외에는 0
- tiny/detail 예외 총 면적이 frozen budget 이내

섬 개수만 낮다고 PASS시키지 않는다.

---

## CG9 — Island Distortion Quality Gate

**필수.**

CG1~CG3을 먼저 통과한 뒤 island 품질을 평가한다.

profile 기반 PASS 조건:

- global anisotropy p95 <= cap
- each island anisotropy p95 <= cap
- global/island area stretch <= cap
- exceed-area fraction <= cap

주의:

- `anisotropy_max` hard failure는 CG2가 담당
- 평균/p95 Gate로 catastrophic failure를 대체하지 않는다.

---

## CG10 — Seam Economy / Merge-back Gate

**필수.**

repair 성공 후 제거 가능한 auxiliary seam이 남아 있으면 최종 결과가 아니다.

PASS 조건:

1. mandatory/user seam은 유지
2. auxiliary seam 하나를 제거해 merge를 시도했을 때
   - CG1~CG3 통과
   - CG8~CG9 통과
   - padding 유지
   되는 경우가 더 이상 존재하지 않음
3. 동등 품질 후보 중
   - island count 최소
   - normalized seam length 최소
   순으로 선택

즉:

```text
품질을 유지하며 합칠 수 있으면 반드시 합친다.
```

---

## CG11 — Game Padding / Texel Gate

**필수.**

PASS 조건:

- island gap >= `margin_px / texture_size_px` 정책
- packing 후 overlap 0
- packing 후 CG1~CG3 재통과
- texel density variance <= frozen profile cap
- rotation/packing 때문에 catastrophic metric이 변하지 않음(균일 scale/rotation invariant 검증)

텍스처 크기와 padding px가 report에 반드시 기록되어야 한다.

---

## CG12 — Export Round-trip Gate

**출시 필수.**

앱 내부 UV가 좋아도 실제 게임 asset export 이후 깨지면 실패다.

지원 포맷별 최소 하나 이상:

- FBX
- glTF/GLB 또는 실제 제품 지원 포맷

PASS 조건:

```text
final in-memory
 -> export
 -> Blender fresh process 또는 독립 reader로 re-read
 -> UV 재추출
 -> CG1, CG2, CG3, CG8, CG9, padding 재검사
```

추가 조건:

- mesh/loop mapping이 export 때문에 변하면 fingerprint 대응표 사용
- export 전후 island topology가 의도치 않게 달라지지 않음
- mandatory seam에 대응하는 UV discontinuity 유지
- final accepted는 round-trip gate 성공 후에만 기록

---

## CG13 — Failure State / Final Emit Gate

**필수.**

복구 불가능한 UV를 최종 결과로 내보내지 않는다.

| 상황 | 기대 상태 |
|---|---|
| CG1 실패 | `needs_user_review` 또는 `failed` |
| CG2/CG3 실패 + budget 소진 | `needs_user_review` |
| candidate 전부 reject | best valid 이전 결과 유지, accepted 금지 |
| quality만 미달 | `needs_user_review` |
| export re-read 실패 | accepted 금지, 이전 승인 asset 유지 |
| 모든 필수 Gate PASS | solver `accepted` 가능 |

`process exit 0`은 acceptance와 무관하다.

preview artifact는 실패 상태에서도 생성 가능하지만 **production export 대상**으로 자동 지정하면 안 된다.

---

## CG14 — Determinism

**필수.**

동일 입력/commit/profile/seed 3회 실행:

- seam set 동일
- bad region face sets 동일
- island count 동일
- candidate acceptance history 동일
- scalar metric 차이 <= `1e-6`

GPU/Blender 연산 특성상 완전 동일 float가 불가능하면 profile에 deterministic tolerance를 명시하고 그 이상 차이는 FAIL.

---

## CG15 — 현재 실패 모델 Regression Gate

**이번 milestone 필수.**

현재 보고된 가시/바늘형 UV 모델은 별도 named regression fixture로 유지한다.

최종 PASS 조건:

- CG1 PASS
- CG2 PASS
- CG3 PASS
- CG8 PASS
- CG9 PASS
- CG10 PASS
- export round-trip PASS
- before/after heatmap 공개
- problematic face set의 catastrophic score가 hard-fail -> pass로 개선
- island 수 증가는 필요한 경우에만 허용하며 merge-back 후 최종 수치를 보고
- reviewer가 checker에서 가시/찢김이 제거되었음을 확인

수치만 통과하고 동일한 시각적 가시가 남으면 PASS 금지.

---

## CG16 — 실모델 Calibration / Holdout

**출시 필수.**

최소 데이터셋:

- hard-surface
- organic
- elongated/cylindrical
- branching shape
- 현재 failure model

calibration 모델과 holdout을 분리한다.

frozen profile 필수 키:

```text
profile_id
catastrophic_metric_version
anisotropy_hard_max
near_collapse_ratio
max_uv_triangle_aspect
local_area_ratio_min
local_area_ratio_max
bad_area_fraction_cap
island anisotropy p95 cap
area stretch caps
margin_px
texture_size_px
tiny/sliver thresholds
regression budgets
iteration/candidate/time/island budgets
```

holdout에서 threshold를 사후 완화하여 개별 모델을 통과시키지 않는다.

---

## CG17 — 전체 회귀

**출시 필수.**

기존 test suite가 전부 통과해야 한다.

특히 회귀 금지:

- preserve_existing mode seam 변경
- user seam lock 무시
- 90도 mandatory seam 누락
- exact overlap audit 약화
- 기존 export contract 파손
- Electron UI/worker mode contract 파손
- deterministic run 파손

신규 test 권장:

```text
tests/test_catastrophic_distortion.py
tests/test_catastrophic_repair.py
tests/test_merge_back.py
tests/test_uv_heatmap_identity.py
tests/e2e/test_catastrophic_uv_regression.py
```

---

# 최종 출시 판정

자동 게임 UV의 최종 승인 조건은 다음 한 문장으로 정의한다.

> **모든 UV triangle이 correctness와 catastrophic hard gate를 통과하고, island 품질·padding·texel 조건을 만족하며, 품질을 유지한 채 제거 가능한 보조 seam이 더 이상 없고, export 재읽기 후에도 동일하게 통과해야 한다.**

이 조건을 만족하지 않으면 solver는 결과 파일을 보여줄 수는 있어도 production-ready UV로 승인해서는 안 된다.
