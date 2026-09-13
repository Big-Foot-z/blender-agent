# 게임용 UV Catastrophic Distortion Acceptance Gate 실행 결과

- 대응 문서: [Acceptance Gates](GAME_UV_CATASTROPHIC_DISTORTION_ACCEPTANCE_GATES.ko.md), [복구 작업 계획서](GAME_UV_CATASTROPHIC_DISTORTION_RECOVERY_PLAN.ko.md)
- 선행 원장: [GAME_UV_GATE_RESULTS.ko.md](GAME_UV_GATE_RESULTS.ko.md) (수정하지 않음)
- 판정 기준 코드: 엔진/worker/app 코드는 `c32e29a` (main). 그 뒤 커밋은 e2e 테스트 timeout 상수(1200/1500 → 3000 s)와 문서·evidence 추가뿐이며 엔진 동작을 바꾸지 않는다.
- 실행 환경: Windows 10 Home 10.0.19045 x64, AMD64 12 core, 49,078 MB RAM, Python 3.14.4 (uv), Node/npm 11, Blender 5.1.2 (ec6e62d40fa9)
- 증거 경로: `docs/evidence/catastrophic_uv_c32e29a/` — 기존 fixture e2e evidence 11개, 실모델 회귀 evidence (`uv_catastrophic_regression_*.json`, `statue_regression/` after 산출물, `statue_before/` 복구 전 heatmap·compact)
- 판정은 메인이 raw 출력(요약/리포트 JSON, pytest/npm 출력)을 직접 대조해 내렸다. 상태값 PASS / FAIL / BLOCKED / NOT_RUN. BLOCKED·NOT_RUN 은 PASS 가 아니다. profile 은 `engineering_v0` (`calibrated = false`).
- **현재 실패 모델(CG0/CG15)**: `tests/e2e/fixtures/real/statue_lowpoly.fbx` (SHA-256 e221cc2c1d51f92f3c99b852bab8b8fd484aadcac5b0560bce2c47a53635bf3f, 358,348 bytes, 5,996 정점 / 11,776 면, decimated statue = 앱 프로젝트 `lowpoly_3/source/original.fbx`). 파일은 사용자 에셋이므로 **저장소 커밋 여부는 사용자 확인 대기 중**이며, 이 원장 작성 시점에는 untracked 로 로컬에만 존재한다(파일 부재 시 회귀 테스트는 skip).

## 요약 표

| Gate | 상태 | 증거 | 비고 |
|---|---|---|---|
| CG0 | PASS (로컬) / 파일 커밋 전까지 타 머신 BLOCKED | `uv_catastrophic_regression_fixture_identity.json`, `run_manifest.json`(model_sha256, mesh fingerprint, code_sha, profile, seed), `uv_hash`, `heatmap_meta.json` | 실패 재현: 복구 전 코드에서 bad triangle 304 / region 66 (`statue_before/`) |
| CG1 | PASS (fixture) / 실모델 FAIL | `correctness.json`, e2e all_fixtures | 실모델은 기존 self-overlap 0.0012 tile area 잔존 |
| CG2 | PASS (fixture) / 실모델 FAIL | `uv_catastrophic.json`, `tests/test_catastrophic_distortion.py` | 실모델 잔존 bad triangle 8 (anisotropy 8.2–28.2, 단일 삼각형 4 region) |
| CG3 | PASS (fixture) / 실모델 FAIL | 동일 | region 4, bad area 5.9e-5 (cap 0.005 이내), boundary spike 0, self-overlap region 0 |
| CG4 | PASS | `heatmap_meta.json`, summary `heatmap_identity.passed = true` (fixture 전부 + 실모델), `tests/test_reporting.py` | heatmap 은 gate 의 per-face score 로 렌더, hard-fail 면은 검정, NaN/inf 는 magenta |
| CG5 | PASS | `candidate_history.json` (R1 `reunwrap` 후보가 R2 보다 먼저), `uv_repair_history.json`, `tests/test_catastrophic_repair.py` | seam 추가 없는 복구(R1)가 실모델에서 채택됨(region cleared 24→0) |
| CG6 | PASS | `tests/test_catastrophic_repair.py` (§6 거절 사유 전부), 실모델 후보 거절 기록 | 채택 조건 11항 모두 구현; 실모델에서 overlap 증가 후보 전부 거절 |
| CG7 | PASS | `tests/test_refinement_loop.py`(예외 주입 후 seam/UV/uv_hash/active layer 동일, restore 해시 검증), `tests/test_catastrophic_repair.py` | 복원 후 `uv_hash` 불일치 시 `RuntimeError` |
| CG8 | PASS (fixture) / 실모델 FAIL | `fragmentation` 블록(px 지표), `tests/test_fragmentation.py` | 실모델 sliver 13(면제 10), min width 0.42 px |
| CG9 | PASS (fixture) / 실모델 FAIL | `quality_report.json` | 실모델 global p95 1.37 통과, anisotropy_max 92.7 > 3.0 |
| CG10 | PASS (fixture) / 실모델 PASS(정의상) | `merge_back_history.json` | 실모델: repair 모드 21회 시험 전부 거절(`catastrophic_worse`/`hard_failures_grew`) → 제거 가능 seam 0, `complete = true` |
| CG11 | PASS | `correctness` island/border gap, `texel_density`, `tests/test_catastrophic_distortion.py`(회전·균일 scale 불변 1e-9) | 실모델 island gap 3.47 px < 4 (overlap 동반 실패라 재배치만으로 미해결) |
| CG12 | PASS | `uv_export_reread.json`(bevel_cube fbx/glb 재읽기 CG1/2/3/8/9/padding 통과), `uv_catastrophic_regression_export.json`(실모델 export 거부: `reread_audit_failed` correctness/catastrophic/fragmentation) | round-trip 실패 시 accepted 금지 동작 확인 |
| CG13 | PASS | 실모델 status `needs_user_review`, fixture suzanne/degenerate needs_user_review, exit 0 ≠ accepted | 복구 불가 결과가 accepted 로 나가지 않음 |
| CG14 | PASS (fixture 3회, 실모델 3회) | `uv_auto_gates_test_determinism_three_runs_*.json`, `uv_catastrophic_regression_three_runs_deterministic.json` | 실모델 3회: seam 1,393 동일, bad face id 8개 동일([77, 435, 543, 6352, 7920, 7950, 8302, 10077]), region 집합·island 37·repair rows·uv_hash 동일; wall 1,386 / 1,359 / 1,329 s |
| CG15 | **FAIL** (개선은 확인, 전체 PASS 아님) / 리뷰어 확인 BLOCKED | `statue_before/`, `statue_regression/` | bad triangle 304 → 8, 바늘형 영역 heatmap 에서 제거, island 80 → 37; CG1/CG2/CG8/CG9 잔존 실패 |
| CG16 | BLOCKED | — | calibration/holdout 데이터·리뷰어 없음. engineering default 값만 기록 |
| CG17 | PASS | `uv run python -m pytest tests --ignore=tests/e2e` exit 0 (skip 3 기존), 기존 e2e 4 모듈 20건 exit 0, `npm run typecheck/test:integration(43/43)/build` exit 0 | preserve 모드·locked seam·mandatory·overlap audit·export contract·UI contract 회귀 없음 |

## 실모델(statue) 복구 전/후

| 항목 | 복구 전 (코드 10840c0, repair 미진입) | 복구 후 (코드 c32e29a) |
|---|---|---|
| status | needs_user_review | needs_user_review |
| catastrophic bad triangle / region / bad area | 304 / 66 / 1.45e-2 | 8 / 4 / 5.9e-5 |
| max anisotropy / max UV aspect | 186.7 / 823 | 92.7 / 114.5 |
| boundary spike region / self-overlap region | 41 / 6 | 0 / 0 |
| island 수 | 80 (cap) | 37 |
| global anisotropy p95 / area stretch mean / p95 | 2.30 / 0.269 / 1.055 | 1.37 / 0.066 / 0.229 |
| worst island anisotropy p95 | 8.50 | 1.85 |
| repair | 0 round | 5 round, R1 reunwrap 1 채택(`slim_iter50_noflip`, region 24→0), 후보 34개 |
| correctness | overlap 0.00327 | overlap 0.00119, island gap 3.47 px |
| fragmentation | tiny 62 / sliver 18 | tiny 23 / sliver 13 (면제 10) / min width 0.42 px |
| merge-back | skipped (quality failed) | repair 모드 21 trial, 0 accepted, removable 0 |
| 시간 / 메모리 | 128 s / 382 MB | 1,354 s / 840 MB |

이미지: `statue_before/selected_heatmap_anisotropy_before_repair.png` vs `statue_regression/selected_heatmap_anisotropy.png` (검정 = hard-fail 삼각형, magenta = 측정 불가). 복구 후 heatmap 에서 부채꼴 바늘 영역은 사라졌고 남은 hard-fail 은 단일 삼각형 4개다. **리뷰어 시각 확인은 없음(BLOCKED)**.

잔존 실패의 원인(raw): island 0(4,084 면)에 기존 self-overlap 0.0012 가 있어 이 island 를 건드리는 모든 재unwrap/relief 후보의 overlap 면적이 미세하게 변하고, CG6 "overlap 증가 0" 규칙에 따라 전부 `correctness_regression` 으로 거절된다(R1 10건, R2 11건). correctness repair 라운드 3회는 다른 island 를 대상으로 했고 1건(normal split, island 16) 만 채택됐다. sliver 13개 중 10개는 mandatory fold 로 둘러싸인 구조적 조각(면제)이고 3개는 overlap 수리 보조 절개의 산물이다. 따라서 이 모델을 PASS 로 만들려면 (a) island 0 의 self-overlap 을 relief seam 으로 먼저 해소하는 correctness-repair 후보(현재는 cap/overlap 규칙에 걸림), (b) sliver 를 만든 overlap 수리 절개의 대안, (c) calibration 에서 tiny/sliver 면제 규칙 결정이 필요하다.

## Fixture 결과 (기존 11 fixture, 코드 c32e29a)

| fixture | status | gate failures | islands | merge-back | catastrophic | 비고 |
|---|---|---|---|---|---|---|
| plane_grid / cube / bevel_cube / cylinder_capped / uv_sphere / torus / concave_plate / angle_boundary / protected_path | accepted | — | 1 / 6 / 4 / 4 / 2 / 3 / 10 / 5 / 1 | no_removable_seam | passed | 이전 원장과 island 수 동일 |
| suzanne | needs_user_review | quality_profile_failed, correctness_failed, reread_audit_failed | 13 (이전 12) | no_removable_seam (repair 모드) | passed | correctness repair 가 normal split 1건 채택 |
| degenerate_input | needs_user_review | input_defects | 10 | no_removable_seam | passed | 입력 결함 진단 |

성능(3회, 고정 하드웨어): suzanne wall median 58.5 s (이전 26.3 s — catastrophic/correctness repair 라운드 추가), torus 24.4 s (이전 19.6 s). 한글 경로·contradictory flags·preserve round trip·bevel_cube export round trip 모두 통과.

## 구현 요약 (Gate 대응)

- `uv_agent/geometry/catastrophic_distortion.py` (CG2/CG3): per-triangle reasons `anisotropy_hard`(> 8.0), `near_collapse`(s2/s1 < 1e-4 또는 UV degenerate), `needle`(UV aspect > 40 이고 3D aspect 의 2배 초과), `area_explosion/collapse`(정규화 면적비 밖 [1/25, 25]), `invalid`(비유한값, 0 치환 금지); island 내 연결 성분 region, boundary spike, self-overlap/flip 플래그, `per_face_score`, `bad_face_ids`. 회전/균일 scale 불변 테스트.
- profile 키(CG16 이름 그대로): `catastrophic_metric_version, anisotropy_hard_max, near_collapse_ratio, max_uv_triangle_aspect, needle_3d_aspect_factor, local_area_ratio_min/max, bad_area_fraction_cap, catastrophic_repair_max_rounds, catastrophic_reunwrap_variants`; CG8: `min_island_width_px(10), min_island_area_px2(100), max_island_bbox_aspect(8), max_island_perimeter_area_ratio(12), max_tiny_island_area_fraction(0.02)`. `time_budget_s` 600 → 1800.
- `chart_uv_agent/catastrophic_repair.py` + `refinement_loop.py` (CG5/CG6/CG7): target 우선순위 collapse/invalid/flip/self-overlap → correctness repair → anisotropy hard → needle → area → distortion; R1 `UNWRAP_VARIANTS`(SLIM 50 iter no_flip, SLIM fill_holes, ABF+minimize) 후 R2 relief seam(bad region → boundary 2-leg cut, crease 선호, fragmentation 예측 penalty, normal split); 채택 규칙 §6 전부; snapshot 에 uv_hash·catastrophic counters, 복원 해시 검증; island cap 은 seam 추가 후보에만 적용; 채택된 R1 은 `unwrap_overrides` recipe 로 기록되어 이후 모든 재unwrap 뒤 재적용(CG14 재현성).
- pipeline (계획서 §11): unwrap → correctness/distortion/catastrophic 측정 → repair(cap 상태에서도 진입) → prune → **post-prune catastrophic pass** → merge-back(repair 모드: 실패 상태에서도 hard failure 집합·catastrophic·correctness·fragmentation 이 악화되지 않는 merge 만 채택) → gap repack → 최종 측정 → artifact → export → 재읽기 동일 gate.
- 산출물(§14): `uv_catastrophic.json`, `uv_repair_history.json`, `heatmap_meta.json`(uv_hash/mesh_fingerprint/metric_version/run_id) 추가; `quality_report.json` 에 layers A–E 와 catastrophic/repair 섹션; `selected_heatmap_anisotropy.png` 는 gate 의 per-face score 로 렌더(hard-fail 검정). 기존 파일명 유지.
- contract/worker/export (CG12/CG13/CG4): auto gate 코드 `catastrophic_failed`, `catastrophic_region_failed`, `catastrophic_invalid`, `heatmap_identity_mismatch`; export 재읽기 audit 에 catastrophic + px fragmentation + uv_hash; 재읽기 실패 포맷은 succeeded 제외.
- UI: Game gates 에 Catastrophic(B)/Repair 행, heatmap 탭 identity 배지·실패 배너·worst region 목록, run view 에 3개 신규 artifact.
- e2e: `tests/e2e/test_catastrophic_uv_regression.py` (fixture identity, 3회 결정성, before/after evidence, export round trip).

## 메인이 내린 설계 판단 (문서에 없던 항목)

1. 기존 `anisotropy_max_max = 3.0`(CG9 품질 게이트)은 유지하고 CG2 hard 8.0 을 별도로 추가했다(완화 없음). CG9 문서가 anisotropy_max 를 CG2 소관으로 두지만 기존 원장 G5 의 필수 키를 제거하지 않았다.
2. `needle` 판정에 3D aspect 대비 계수(2.0)를 추가했다: decimation 산물인 3D sliver 삼각형이 등각으로 매핑된 경우(anisotropy ≈ 1)는 UV 실패가 아니다. 실모델에서 오탐 11 region 제거.
3. repair 순서는 계획서 §7 을 따르되 exact self-overlap/flip 은 `correctness_repair` target(기존 절개 경로)으로 처리하고, anisotropy hard/needle 보다 먼저 둔다.
4. island cap 은 seam 을 추가하는 후보에만 적용한다. R1 재unwrap 은 cap 상태에서도 실행한다.
5. 채택된 R1 변형은 `unwrap_overrides`(face set → variant) 로 기록되고 같은 seam 집합의 모든 재unwrap 뒤 재적용된다. island 구성이 바뀌면 stale 로 기록되고 재적용하지 않는다.
6. prune 뒤 catastrophic 이 남아 있고 island 수가 cap 미만이면 2차 repair pass 를 실행한다(`catastrophic_repair_max_rounds`).
7. merge-back 은 실패 레이아웃에서도 "repair 모드"로 실행한다: hard failure 집합이 커지지 않고 catastrophic/correctness/fragmentation 카운터가 악화되지 않으며 island 가 1 줄어드는 merge 만 채택(tiny/sliver 를 포함한 쌍 우선). 통과 레이아웃에서는 기존과 동일.
8. `time_budget_s` 공학 기본값을 600 → 1800 s 로 올렸다(11k 면 실모델의 후보당 10–15 s). fixture 는 60 s 내 종료라 기존 증거에 영향 없음.
9. 산출물 파일명은 기존 이름을 유지하고 `uv_catastrophic.json`/`uv_repair_history.json`/`heatmap_meta.json` 을 추가했다(계획서 §14 의 `uv_quality.json` 등은 `quality_report.json` 등에 대응).
10. OBJ round-trip 은 제품 지원 범위 밖으로 실행하지 않았다(FBX/GLB).
11. 실패 모델 fixture 는 사용자 에셋(fbx, 358 KB)이므로 커밋을 사용자 확인 사항으로 남겼다.
12. CG7 의 UV 비교 tolerance 는 정확 일치(uv_hash, float64 바이트)다.
13. e2e 회귀 테스트의 worker timeout 을 1,200/1,500 s → 3,000 s 로 올렸다(실모델 1회 실행 약 1,360 s).

## 다음 단계 (원장 판정과 별개의 관찰)

- 실모델 PASS 를 막는 것은 island 0 의 기존 self-overlap 과 overlap 수리 절개가 만든 sliver 다. §7 의 "self-overlap region" 을 relief seam 으로 직접 해소하는 후보(현재 correctness_repair 는 island 단위 split)와, sliver 를 만드는 절개의 대안 경로가 다음 milestone 후보다.
- calibration(CG16) 없이는 tiny/sliver 면제·hard 임계값을 고정할 수 없다.
