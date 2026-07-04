# Reforge: 3D 뷰어 + Blender 라이브 연동 계획

대상: `app/` Electron 앱 (Prepare 워크스페이스 중심) + `worker/` Blender 워커 + 신규 Blender 브리지 애드온.

## 0. 배경 / 요구사항

1. **3D 뷰어** — Prepare(로우폴리 가져오기/생성) 화면 가운데 뷰어가 현재 `preview.png` 정적 이미지(`PrepareWorkspace.tsx`의 `PreviewPane`)라 평면이다. Blender처럼 3D로 보고 카메라(orbit/pan/zoom)를 움직일 수 있어야 한다.
2. **Blender 라이브 갱신** — 로우폴리를 생성·승인(저장)했을 때, 사용자가 Blender에 해당 프로젝트의 fbx/blend를 이미 띄워놓았으면 그 씬을 자동/원클릭으로 갱신한다. 수동 재-import 왕복 제거가 목적.
3. **"블렌더로 바로 띄우기" 버튼** — 승인된 working model(.blend 우선, 없으면 .fbx)을 설정된 `blenderPath`로 바로 연다.

## 1. 현재 구조 (근거)

- `PreviewPane`(app/electron/renderer/src/PrepareWorkspace.tsx:235) — `uvpreview://<preview_path>` PNG `<img>` 하나가 전부.
- `SeamViewport.tsx` — Three.js LineSegments + **자체 orbit/pan/zoom 카메라**(OrbitControls 미사용)가 이미 검증돼 있음. 단 edge-only 지오메트리라 면(셰이딩) 렌더 불가.
- `run_app_retopo_job.py` — 런 산출물로 `lowpoly.blend`, `lowpoly.fbx`, `preview.png` (모두 best-effort). GLB 산출물은 없음.
- `approveLowpoly`(project-service.ts) — `work/working_lowpoly.blend` (+ `.fbx`) 복사 후 manifest 갱신. 여기가 Blender 갱신 트리거 지점.
- `settings.blenderPath` — 네이티브 파일 다이얼로그로 설정됨. 워커 실행에 이미 사용.
- 실행 중 Blender 인스턴스를 밖에서 조작하는 공식 CLI는 없음 → 애드온(소켓 서버) 필요.

## 2. Phase 1 — Prepare 3D 뷰어 (핵심)

### 2.1 워커: `lowpoly.glb` 산출물 추가

- `run_app_retopo_job.py`의 `.blend`/`.fbx` 저장 직후 `bpy.ops.export_scene.gltf(filepath=out_dir/"lowpoly.glb", use_selection=True, export_format='GLB')` 추가 (best-effort, 기존 패턴과 동일하게 실패해도 런은 성공).
  - glTF 익스포터는 Blender 번들 애드온이며 background 모드에서 동작(워커가 이미 `import_scene.gltf`를 쓰고 있어 가용성 확인됨).
  - 12k면 수준이라 GLB 용량/시간 부담 없음. Draco 불필요.
- `summary.artifacts`에 `lowpoly_glb: "lowpoly.glb"` 등록 → `RunView`에 자동 노출 (project-service의 artifacts 루프가 이미 일반화돼 있음).
- 계약: `shared/contracts`의 RunView/summary 타입에 `lowpoly_glb` 선택 필드 반영.

### 2.2 렌더러: `ModelViewport` 컴포넌트 (신규)

`app/electron/renderer/src/viewport/ModelViewport.tsx`:

- **로딩**: `fetch('uvpreview://<abs path to lowpoly.glb>')` → `arrayBuffer` → `GLTFLoader.parse`. (기존 커스텀 프로토콜 재사용; GLB MIME 이슈 없는지 main의 protocol 핸들러 확인, 필요시 `model/gltf-binary` 헤더 추가.)
- **카메라**: `SeamViewport`의 orbit/pan/zoom 코드를 `viewport/useOrbitCamera.ts`(또는 `orbitCamera.ts` 순수 모듈)로 추출해 양쪽에서 공유. 조작계는 Blender 유사: 드래그=orbit, Shift+드래그/우클릭=pan, 휠=zoom, Reset 버튼. (SeamViewport는 이번에 리팩터만, 동작 불변.)
- **씬**: MeshStandardMaterial(단색, 러프 0.8) + HemisphereLight + DirectionalLight, 그리드 바닥(`GridHelper`) + 축(`AxesHelper`), 유닛 구 정규화는 SeamViewport와 동일한 방식.
- **토글**: 와이어프레임 오버레이(on/off), 플랫/스무스 셰이딩, 그리드 on/off. HUD에 면/버텍스 수.
- **정리**: 지오메트리/머티리얼 dispose, ResizeObserver — SeamViewport 패턴 그대로.

### 2.3 PrepareWorkspace 통합

- `PreviewPane` → 탭 형태로 교체: **[3D 뷰] [렌더 이미지]**.
  - `lowpoly_glb` 아티팩트가 있으면 3D 뷰 기본, 없으면(과거 런) 기존 PNG로 폴백.
  - 런 진행 중(터미널 상태 전)은 기존 placeholder 유지.
- i18n 문자열 추가 (en/ko).

### 2.4 (Phase 4, 선택) 소스 모델 3D 뷰

Import/Inspect 직후 원본을 3D로 보고 싶다는 요구가 나오면: `inspect_model.py`에 옵션으로 뷰 프록시 GLB(데시메이트 ~150k tri) 산출 추가. 원본이 수백만 면일 수 있어 기본 off. 이번 범위에서는 제외하고 별도 판단.

## 3. Phase 2 — "블렌더로 바로 띄우기" 버튼

### 3.1 Main 프로세스: `blenderLauncher.ts` (신규)

- `blenderOpen({ projectId })`:
  - 대상 해석: `working_model`(.blend) 우선, 없으면 `working_model_fbx`.
  - `.blend`: `spawn(blenderPath, [absBlend], { detached: true, stdio: 'ignore' })`.
  - `.fbx`: 번들 스크립트 `worker/open_in_blender.py`로 `spawn(blenderPath, ['--python', script, '--', absFbx])` — 새 씬에서 기본 큐브 제거 후 `import_scene.fbx`. (`--python-expr` 대신 스크립트 파일: Windows 인용 문제 회피, 패키징 시 `pysrc/`에 포함.)
  - `blenderPath` 미설정이면 에러 배너 + 설정 유도.
- IPC/preload: `window.api.blenderOpen(...)` 추가, `ipc.ts`에 핸들러 등록.

### 3.2 UI

- Prepare `RightPanel`의 승인 섹션(+승인 직후 배너)에 **"Blender에서 열기"** 버튼.
- Export 워크스페이스에도 동일 버튼(내보낸 파일 대상)은 후속으로 쉽게 확장 가능 — 이번엔 Prepare만.

## 4. Phase 3 — Blender 브리지 애드온 + 승인 시 라이브 갱신

### 4.1 애드온 `reforge_bridge` (신규, `worker/addon/reforge_bridge.py`)

- localhost TCP 서버(기본 포트 43717, 점유 시 +1 스캔). 백그라운드 스레드는 소켓 수신만 하고, 명령 실행은 `bpy.app.timers`로 **메인 스레드에 마샬링**(bpy는 메인 스레드 전용).
- 기동 시 핸드셰이크 파일 `~/.reforge/bridge.json`에 `{ port, token, blender_version, pid }` 기록, 종료 시 삭제. 앱은 이 파일로 발견 + 토큰 검증(로컬 포트 하이재킹 방지).
- 프로토콜: JSON Lines 요청/응답. 명령:
  - `ping` → `{ok, file: bpy.data.filepath, dirty: bpy.data.is_dirty}`
  - `open_file {path}` — blend는 `wm.open_mainfile`, fbx는 새 씬 import. import한 오브젝트에 커스텀 프로퍼티 `reforge_project`, `reforge_source` 태깅.
  - `refresh {project_dir, blend_path, fbx_path}`:
    1. 현재 파일 == `blend_path`(정규화 비교) → `wm.revert_mainfile`.
    2. 아니고 `reforge_source == fbx_path` 태그가 붙은 오브젝트가 씬에 있으면 → 해당 오브젝트(+메시 데이터) 삭제 후 fbx 재-import, 태그 재부여. (이름 `.001` 문제를 태그 추적으로 회피.)
    3. 둘 다 아니면 `{ok: false, reason: 'not_loaded'}` — 앱은 "열려있지 않음" 안내.
  - **unsaved 보호**: `refresh`가 revert 경로일 때 `bpy.data.is_dirty`면 즉시 revert하지 않고 `{ok:false, reason:'dirty'}` 반환 → 앱에서 "Blender에 저장 안 된 변경이 있어 갱신 보류" 배너. (사용자 데이터 파괴 금지가 v1 원칙.)

### 4.2 애드온 설치 UX

- 애드온 파일을 `extraResources`(패키징 시 `pysrc/addon/`)로 번들.
- 설정 바에 **"Blender 브리지 설치"** 버튼: `blender --background --python install_bridge.py` 실행 → `bpy.ops.preferences.addon_install(filepath=...)` + `addon_enable` + `wm.save_userprefs`. 버전별 addons 경로 추측 불필요, 기존 `blenderPath` 재사용.
- 설치 후 Blender 재시작(또는 새로 열기) 안내.

### 4.3 앱 측 `blenderBridge.ts` (main, 신규)

- `bridgeStatus()`: 핸드셰이크 파일 읽기 → `ping` 시도 → `{connected, file, dirty}`.
- `bridgeRefresh({projectId})`: working blend/fbx 절대경로로 `refresh` 전송, 결과 반환.
- 승인 훅: `lowpolyApprove` IPC 성공 후, 설정 `autoRefreshBlender`(기본 on)이고 브리지 연결돼 있으면 자동 `refresh` → 결과를 배너로 ("Blender 씬 갱신됨" / "보류: 저장 안 된 변경" / 브리지 없음이면 조용히 스킵).
- UI: Prepare 승인 섹션에 브리지 상태 인디케이터(연결됨/꺼짐) + 수동 "Blender 갱신" 버튼.
- Settings 계약에 `autoRefreshBlender: boolean` 추가.

## 5. 테스트 / 검증

- **순수 로직 단위 테스트**(vitest): JSON-lines 프레이밍/토큰 검증, 핸드셰이크 파일 파싱, refresh 대상 경로 해석. 브리지 클라이언트는 mock TCP 서버로 통합 테스트.
- **워커**: 기존 통합 테스트 패턴에 `lowpoly_glb` 아티팩트 존재 assert 추가 (Blender 필요 테스트는 기존 게이팅 관례 따름).
- **Blender e2e (수동, Blender 5.0.1)**:
  1. pottery 샘플 생성 → 3D 뷰 orbit/pan/zoom/와이어 토글 확인, PNG 폴백(구 런) 확인.
  2. "Blender에서 열기" — blend/fbx 각각.
  3. 브리지 설치 → Blender에서 working blend 열기 → 앱에서 재생성+승인 → 자동 갱신 확인; dirty 상태 보류 확인; fbx import 경로(태그 재-import) 확인.
- 패키징: 애드온/`open_in_blender.py`가 `pysrc/`에 포함되는지 `resolveWorkerRoot` 경로로 확인 (mac 우선, win은 기존 관례대로 미검증 표기).

## 6. 리스크 / 결정 사항

- **GLB export 실패**(익스포터 비활성 등) → best-effort + PNG 폴백이라 런 자체는 영향 없음.
- **fbx 재-import 시 머티리얼/트랜스폼 차이** — v1은 "지오메트리 갱신"이 목적; import 기본 설정 사용, 알려진 한계로 문서화.
- **다중 Blender 인스턴스** — 핸드셰이크 파일은 최후 기동 인스턴스 기준(v1 단일 인스턴스 가정, 문서화).
- **방화벽/보안** — 127.0.0.1 바인드 + 토큰. 외부 노출 없음.
- SeamViewport 카메라 추출은 **동작 불변 리팩터** — seam 편집 회귀 없어야 함(기존 seam 테스트로 커버).

## 7. 작업 순서 요약

| 순서 | 내용 | 산출물 |
|---|---|---|
| P1 | GLB 아티팩트 + ModelViewport + Prepare 탭 통합 | 3D 뷰어 동작 |
| P2 | blenderLauncher + "Blender에서 열기" 버튼 | 원클릭 열기 |
| P3 | reforge_bridge 애드온 + 설치 버튼 + 승인 자동 갱신 | 라이브 갱신 |
| P4(선택) | Inspect 시 소스 프록시 GLB 3D 뷰 | 요구 발생 시 |
