# 게임용 UV Catastrophic Distortion 복구 작업 계획서

> 대상 저장소: `Big-Foot-z/blender-agent`
> 문서 성격: **기존 GAME_UV_* 작업 완료 이후의 신규 후속 작업 계획**
> 목표: 현재 자동 UV가 생성은 되지만 checker/heatmap에서 바늘·가시·찢김 형태의 극단 왜곡이 남는 문제를 자동으로 탐지하고, 실패 결과를 그대로 노출하지 않고 복구 가능한 경우 자동 복구한다.
> 핵심 원칙: 기존 seam core / distortion v2 / correctness / refinement loop를 폐기하지 않는다. 이번 작업은 **catastrophic triangle/region 검출 + repair 전용 단계 + 후보 rollback + final emit gate**를 추가하는 후속 단계다.

---

## 1. 문제 정의

현재 파이프라인에는 이미 다음 기반이 있다.

- `artist_uv_agent/seam_policy.py`
  - 90도 이상 fold를 mandatory seam으로 취급
  - low-angle edge 보존 bias
  - user forbidden/preferred constraint
- `artist_uv_agent/seam_refinement.py`
  - worst island 기반 distortion refinement helper
- `uv_agent/geometry/distortion_v2.py`
  - triangle Jacobian singular value 기반 anisotropy
  - global / island / region 통계
  - UV degenerate triangle 보고
- `uv_agent/geometry/uv_correctness.py`
  - exact overlap
  - orientation / local flip
  - degenerate UV
  - bounds / island gap
- `chart_uv_agent/refinement_loop.py`
  - propose -> apply -> measure -> accept/revert
  - one-island-per-round
  - snapshot restore
- `chart_uv_agent/quality_profile.py`
  - anisotropy / area stretch / exceed-area cap
  - candidate regression budget

즉, 이번 문제는 "왜곡 측정 기능이 전혀 없다"가 아니다.

문제는 최종 결과에서 아래와 같은 **catastrophic local failure**가 여전히 남을 수 있다는 점이다.

```text
정상 island 내부
  -> 일부 triangle 또는 작은 face cluster가 극단적으로 길어짐
  -> checker가 가시/바늘/찢어진 부채꼴처럼 보임
  -> island 평균 또는 p95만으로는 사용자가 보는 시각적 실패를 충분히 설명하지 못함
  -> refinement가 seam을 늘리더라도 cut path가 나쁘면 다른 catastrophic region이 생김
```

게임용 UV에서 이 상태는 수치상 일부 metric이 개선되더라도 최종 산출물로 허용할 수 없다.

---

## 2. 이번 작업의 목표

이번 milestone은 다음 네 가지를 동시에 해결해야 한다.

1. **Catastrophic distortion을 triangle/region 단위로 확실히 검출한다.**
2. **실패 원인이 seam 부족인지 unwrap 실패인지 구분한다.**
3. **repair를 시도하되, 악화되면 UV와 seam을 완전히 되돌린다.**
4. **복구하지 못한 결과를 accepted/final asset으로 내보내지 않는다.**

이번 작업의 성공 기준은 "빨간 영역이 적어졌다"가 아니다.

```text
극단적으로 늘어난 UV triangle이 hard gate를 통과하지 못하고,
복구 가능한 경우 자동으로 정상화되고,
복구 불가능하면 needs_user_review로 종료되는 것
```

이다.

---

## 3. 하지 않을 것

이번 작업에서는 다음을 하지 않는다.

- 새 UV engine을 추가하지 않는다.
- `chart_uv_agent` 전체를 다시 작성하지 않는다.
- 90도 mandatory seam 규칙을 제거하지 않는다.
- LLM이 직접 seam edge를 임의 생성하게 하지 않는다.
- 평균 distortion만 낮추기 위해 island를 무제한 분할하지 않는다.
- packing 효율을 위해 distortion repair seam을 추가하지 않는다.
- 실패 결과를 preview가 있다는 이유만으로 성공으로 취급하지 않는다.

---

## 4. 핵심 설계 변경

### 4.1 최종 품질 판단 순서를 바꾼다

기존의 "distortion을 보고 split" 흐름 위에 아래 계층을 명시적으로 둔다.

```text
A. UV Correctness
   - finite
   - bounds
   - exact overlap
   - local flip
   - UV collapse

B. Catastrophic Triangle / Region
   - extreme anisotropy
   - needle/sliver UV triangle
   - near-collapse
   - local area explosion/collapse
   - boundary spike / self-cross style region

C. Island Quality
   - anisotropy p95
   - area stretch
   - exceed-area fraction

D. Seam Economy
   - island count
   - auxiliary seam length
   - merge-back 가능 여부

E. Game Production
   - padding
   - texel density
   - export round-trip
```

A/B가 실패하면 C/D의 평균값이 좋아도 최종 결과는 실패다.

---

## 5. 신규 구현 단위

### 5.1 `uv_agent/geometry/catastrophic_distortion.py`

신규 모듈을 추가한다.

목적: `distortion_v2`가 계산하는 per-triangle 기하를 재사용하여 **게임용 hard-failure region**을 만든다.

권장 데이터 구조:

```python
@dataclass(frozen=True)
class CatastrophicThresholds:
    anisotropy_hard_max: float = 8.0
    anisotropy_warn: float = 4.0
    min_relative_singular_value: float = 1e-4
    max_uv_triangle_aspect: float = 40.0
    max_local_area_ratio: float = 25.0
    min_local_area_ratio: float = 1.0 / 25.0
    max_bad_area_fraction: float = 0.01
    min_cluster_area_fraction: float = 1e-5

@dataclass
class BadTriangle:
    face_id: int
    loop_ids: tuple[int, int, int]
    anisotropy: float
    singular_value_min: float
    uv_aspect_ratio: float
    normalized_area_ratio: float
    reasons: list[str]

@dataclass
class BadRegion:
    region_id: int
    face_ids: list[int]
    area_fraction: float
    max_anisotropy: float
    max_uv_aspect_ratio: float
    reasons: list[str]
```

초기 threshold는 engineering default이며 출시 전에 fixture/실모델로 calibration한다.

#### 검출 대상

- `s2 / max(s1, eps)`가 지나치게 작음
- anisotropy가 hard max 초과
- UV triangle 자체가 지나치게 needle-like
- global area normalization 이후 triangle area ratio가 극단적으로 큼/작음
- 같은 island 안에서 bad triangle이 연속 cluster를 형성
- UV boundary에 매우 뾰족한 spike를 만드는 연속 edge cluster

`anisotropy_max` 하나만 보고 끝내지 말고 **어느 triangle/face cluster가 실패했는지** 반환해야 한다.

---

### 5.2 `chart_uv_agent/catastrophic_repair.py`

신규 repair 단계.

입력:

```text
mesh
current seams
current uvmap
bad regions
constraints
quality profile
repair budget
```

출력:

```text
best seams
best uvmap
repair history
termination reason
```

repair 유형을 두 종류로 분리한다.

#### R1 — Re-unwrap without new seam

먼저 같은 seam 집합으로 unwrap 방법/핀/relax 조건 변경을 시도한다.

이유: catastrophic distortion이 항상 seam 부족 때문은 아니다.

후보 예:

- SLIM / minimum stretch 재실행
- local relax
- island rotation/scale 정규화 후 재평가
- strip/grid/cylinder 형태라면 해당 projection helper 사용 가능 여부 확인

새 seam 없이 hard gate가 통과하면 그것을 우선한다.

#### R2 — Relief seam candidate

R1이 실패한 경우에만 bad region을 해소하는 seam 후보를 만든다.

후보 경로는 다음을 만족해야 한다.

```text
bad region
  -> 가까운 existing seam / island boundary / topology boundary로 연결
```

경로 cost:

- mandatory seam: 이미 seam이면 cost 0
- hidden/back/concave edge: 낮은 cost
- moderate crease: 중간 cost
- smooth visible edge: 높은 cost
- protected/forbidden edge: 통과 금지
- tiny island를 만드는 cut: 매우 큰 penalty
- sliver island를 만드는 cut: 매우 큰 penalty
- seam length 증가량: penalty

한 bad region당 최소 2~4개 후보를 비교하고 deterministic ranking을 사용한다.

---

## 6. 후보 채택 규칙 변경

기존 "target metric 15% 개선"만으로는 부족하다.

새 후보는 아래 조건을 **전부** 만족해야 채택한다.

```text
1. mandatory seam 유지
2. forbidden/protected constraint 위반 0
3. exact overlap 악화 없음
4. local flip 증가 없음
5. UV degenerate 증가 없음
6. catastrophic triangle count 감소
7. catastrophic bad-area fraction 감소
8. target region 품질이 최소 개선율 이상 개선
9. 새로운 tiny/sliver island 생성 금지
10. island cap 미초과
11. 비-target 품질 지표가 regression budget 이내
```

특히 다음은 강제 reject다.

```text
bad_region_count가 줄었지만 anisotropy_hard_max가 더 커짐
bad_area_fraction이 줄었지만 local flip이 새로 생김
평균 distortion은 좋아졌지만 needle triangle이 새로 생김
island 수만 늘고 hard failure가 그대로임
```

---

## 7. Repair 우선순위

한 round에서 하나의 target region만 처리한다.

우선순위:

```text
1. UV degenerate / near-collapse
2. local flip
3. exact self-overlap에 관여하는 region
4. anisotropy hard max 초과
5. needle/sliver UV triangle cluster
6. local area explosion/collapse
7. island p95 / mean distortion
```

즉, 사용자가 눈으로 보기에 "가시처럼 찢어진 부분"을 평균 distortion보다 먼저 잡는다.

---

## 8. Merge-back 단계 추가

Catastrophic repair가 끝난 뒤에는 불필요한 seam을 다시 줄인다.

신규 함수 권장:

```text
chart_uv_agent/merge_back.py
```

처리:

1. auxiliary seam 중 mandatory/user seam이 아닌 것만 후보화
2. 인접 island pair를 seam 제거 후 임시 merge
3. unwrap + 전체 hard gate 재평가
4. hard gate 통과 + quality profile 통과 시 merge 유지
5. seam 길이 / island 수를 줄이는 방향으로 반복

목표는 다음과 같다.

```text
repair를 위해 잠시 더 잘랐더라도,
품질을 유지할 수 있으면 최종 결과에서는 다시 붙인다.
```

이 단계가 없으면 catastrophic repair가 over-segmentation으로 바뀔 수 있다.

---

## 9. Tiny / Sliver island 정책

게임용 UV에서는 island 개수 자체보다 **실제 pixel usability**가 중요하다.

신규 metric 권장:

- `island_area_px`
- `island_min_width_px`
- `island_bbox_aspect`
- `island_perimeter_area_ratio`
- `tiny_island_count`
- `sliver_island_count`

texture size 기준으로 계산한다.

engineering default 예:

```text
texture_size = profile.texture_size_px
minimum usable island width = max(2 * margin_px + 2, 6px)
```

단, 명시적으로 `detail`로 분류된 island는 별도 profile을 적용할 수 있다.

---

## 10. Heatmap과 report 일치 보장

현재 디버깅에서 가장 위험한 상태는 **report는 PASS인데 heatmap은 명백히 붉은 spike를 보여주는 경우**다.

따라서 다음 contract를 추가한다.

- heatmap의 triangle score source는 final gate가 사용하는 동일 per-triangle metric이어야 한다.
- NaN / +Inf / -Inf를 시각화 과정에서 0으로 바꾸지 않는다.
- invalid/degenerate triangle은 별도 색/flag로 렌더링한다.
- preview와 report에 동일 `run_id`, `mesh_fingerprint`, `uv_hash`, `metric_version`을 기록한다.
- final UV 재측정 결과와 preview가 같은 UV hash를 가져야 한다.

즉 **시각화와 gate가 서로 다른 데이터를 보고 판단하는 상태를 금지**한다.

---

## 11. 파이프라인 통합 위치

권장 최종 흐름:

```text
initial seams
 -> unwrap
 -> correctness audit
 -> distortion v2
 -> catastrophic audit
 -> if hard failure:
      catastrophic repair loop
 -> island-quality refinement
 -> merge-back
 -> pack
 -> final correctness
 -> final catastrophic audit
 -> final game-quality gate
 -> write artifacts
 -> export
 -> re-read
 -> same gates again
```

기존 `chart_uv_agent.refinement_loop`는 유지한다.

구현 선택지는 두 가지다.

### 권장

`refinement_loop.py` 안에서 target kind를 확장한다.

```text
catastrophic_repair
distortion
correctness_repair
```

### 비권장

별도 전체 UV pipeline을 새로 만드는 것.

---

## 12. 구현 단계

### Phase C0 — 실패 재현 fixture 고정

현재 실패 모델을 regression fixture로 보존한다.

필수 저장:

- 원본/low-poly model hash
- seam set
- UV map 또는 재현 seed/config
- final heatmap
- quality/correctness JSON
- problematic face ids

**완료 조건:** 동일 commit에서 동일 실패가 deterministic하게 재현된다.

### Phase C1 — Catastrophic metric

`catastrophic_distortion.py` 구현.

**완료 조건:** synthetic needle/collapse/stretch fixture에서 의도한 triangle이 정확히 잡힌다.

### Phase C2 — Gate wiring

quality report에 catastrophic summary를 연결.

**완료 조건:** 기존 결과 중 spike fixture가 무조건 fail 한다.

### Phase C3 — Repair without new seam

재unwrap/relax 후보 우선 적용.

**완료 조건:** seam 추가 없이 해결 가능한 fixture에서 island count 증가 0.

### Phase C4 — Relief seam path search

bad region 기반 seam 후보 생성 및 ranking.

**완료 조건:** 단순 worst-island 임의 split보다 bad area와 seam length가 동시에 개선된다.

### Phase C5 — Candidate rollback 강화

catastrophic counter까지 snapshot 비교에 포함.

**완료 조건:** 실패/예외 후보 후 seam+UV+active layer+metrics baseline이 정확히 복원된다.

### Phase C6 — Merge-back

repair 후 불필요 seam 제거.

**완료 조건:** hard/quality gate 유지 상태에서 제거 가능한 auxiliary seam이 0개 남는다.

### Phase C7 — Export round-trip

FBX/OBJ/glTF 지원 경로에서 export -> reread 후 동일 gate 실행.

**완료 조건:** 앱 내부 PASS가 export 재읽기에서 FAIL이면 final accepted가 되지 않는다.

---

## 13. 테스트 계획

필수 synthetic fixture:

- conformal plane
- 4x / 0.25x anisotropic triangle
- one-needle triangle inside otherwise good island
- UV collapsed triangle
- flipped triangle
- self-overlap island
- thin cylinder with one bad seam
- branching Y/T shape
- long strip
- tiny detail island
- forced protected-edge detour

실모델:

- 현재 실패 모델
- hard-surface 1개 이상
- organic 1개 이상
- cylindrical/elongated 1개 이상

테스트는 숫자만 확인하지 않는다.

각 실패 fixture에 대해:

```text
before bad triangles
before bad area
before island count
before seam length
repair action
accepted/rejected reason
after bad triangles
after bad area
after island count
after seam length
```

을 artifact로 저장한다.

---

## 14. 산출물

모든 auto_generate 실행은 최소 다음을 남긴다.

```text
uv_quality.json
uv_correctness.json
uv_catastrophic.json
uv_repair_history.json
uv_seam_report.json
uv_heatmap.png
uv_checker.png
uv_layout.png
```

`uv_catastrophic.json` 필수 필드:

```json
{
  "metric_version": 1,
  "hard_failed": true,
  "bad_triangle_count": 0,
  "bad_region_count": 0,
  "bad_area_fraction": 0.0,
  "max_anisotropy": 0.0,
  "max_uv_triangle_aspect": 0.0,
  "near_collapse_count": 0,
  "regions": []
}
```

---

## 15. 최종 구현 원칙

이 작업의 가장 중요한 판단 기준은 다음이다.

```text
평균이 좋아졌는가?                -> 부족함
p95가 좋아졌는가?                 -> 부족함
island 수가 줄었는가?             -> 부족함

사용자가 보는 최악의 지역이
실제로 게임용 UV로 사용 가능한가? -> 최종 판단
```

최종 solver는 **최악의 triangle/region을 외면하지 않고**, 해결할 수 없으면 명확히 실패해야 한다.
