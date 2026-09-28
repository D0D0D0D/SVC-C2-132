# SVC-C2-132 設計仕様書 — Game Live-Ops Player-Promise Impact Review Agent

> This public copy records the agent's scope and implementation decisions.
> Cat 2 / SVC / 蓄積型（accumulation-type）/ AgentBaseGraph（L1 直継承）+ main に GraphNode（内側 domain workflow）

## Position in AgentCore Architecture

- **Agent Class**: `PlayerPromiseImpactReviewGraph`（`src/graph/graph.py`・`AgentBaseGraph` を L1 直継承）
- **L1 Base**: AgentBaseGraph（**AutonomousBaseGraph ではない** — 固定 5 ノード backbone。自律ループ不要）
- **Composition**: main スロットに `PromiseReviewGraphNode`（`GraphNode`）を置き、内側の
  `PromiseReviewWorkflowGraph`（`src/graph/domain_workflow_graph.py`）で多段ドメインワークフローを実行する
  （Cat 2 の標準構成）。継承欄は `AgentBaseGraph` のみ。GraphNode / inner graph は「継承」ではなく
  「構成（composition）」であり、下の Composition Pattern 節で扱う。
- **Three-Layer Separation**:
  - State: flat TypedDict composition（Pydantic 不可 — msgpack 非互換）
  - Node: L1 inheritance（Template Method: `execute(self, state) -> dict` のみ override・**部分 dict 返却**）
  - Graph: composition（`register_nodes()` で node 登録・`add_edges()` は override しない）
- **status の値**: 全ノードの返却で `status` は **`AgentStatus.<X>.value`（文字列）**で設定する
  （bare Enum 返却は規約違反）。route() は main 後に `state["status"]` を読む
  （`AgentStatus.SUCCESS.value`→post / `AgentStatus.RETRY.value`→pre / `AgentStatus.ERROR.value`→finalize）。

**Entry points.** Two adapters, no business logic in either: `src/api/server.py` (standalone
FastAPI `/invoke`) and `cli.py` (AGENTIC STAR Marketplace). The Marketplace adapter forces `mode:
invoke`, supplies the tenant/org scope from `ASTAR_TENANT_ID`/`ASTAR_ORG_ID` with
`permissions: ["read"]` only (so `ingest`/`feedback` stay unreachable through the
Marketplace facade, which cannot reach an internal trust level), and
installs the Azure client into the `src/services/llm_seam` module seam. See
`docs/07_operation_guide.md`.

## 1. このエージェントは何をするか（設計の核）

モバイル/オンラインゲームの **ライブ運営（live-ops）担当**が、期間限定イベント（報酬・当選条件・
プレイヤー向け告知文）をリリースする **前** に使う、蓄積型のレビューエージェント。`/invoke` は read-only、
`/ingest` `/feedback` は統治された裁定 KB への限定書込を行う。

価値の核は「ID・version の証跡突合」ではなく、**プレイヤー向け告知が約束している内容と、
versioned config が実際に実行する報酬条件との「意味的な乖離」を、version・受益者影響つきで検出する
（Player-Promise Impact Review）** ことにある。

- **決定論に落ちる部分（内部 Tool として従属）**: ID・報酬テーブル・version の突合、承認記録の有無判定。
- **LLM が必須の部分**: 「イベント期間中いつでも入手可能」という告知文が、実ルール（1日1回・在庫上限・
  配布は翌日）を実質的にどう約束しているか、という自由記述の告知表現 → 実行条件への当てはめ判定。

## 2. モード（単一グラフ・mode 分岐＝蓄積型のデータ資産ライフサイクル）

蓄積型 AG のデータフライホイールを 1 グラフの mode 分岐で担う（別グラフを立てない＝AgentCore single-entry と整合・
Single-responsibility は「同一データ資産のライフサイクル管理」に収まり Dual-mode 回避）。

| mode | 種別 | 入力 | 出力 |
|---|---|---|---|
| `ingest` | 書込 | 過去の告知修正・差戻し（告知文・実ルール・乖離指摘・修正内容・裁定理由・裁定結果） | 取込件数 |
| `invoke` | read-only | read-only の event-config export・承認済み報酬/eligibility 表・変更依頼・告知文 | source-linked exception packet ＋ 人間レビュー質問 |
| `feedback` | 書込 | マネージャーが実際にどう対応したか（修正/そのまま/差戻し）＋確定裁定 | 記録結果 |

**mode の正準表記**: State の `mode` フィールドは **bare 文字列 `"invoke"`/`"ingest"`/`"feedback"`**（先頭スラッシュ無し）。
`/invoke` 等は API ルート表記であり、pre_process が正準化して State に格納する。`mode` が上記 3 値以外なら pre_process が
即 `status = AgentStatus.ERROR.value`（unknown-mode reject・fail-closed）。

### 2-1. mode 別ペイロード契約（closed schema・executable contract）

各 mode の入力は **closed typed schema**（許可キーのみ・未知キー reject・型不一致 reject）。pre_process の
`_extra_security_gate_input()` で検証し、違反は `SecurityViolationError`（→ status=error）。

| mode | 必須キー | 任意キー（既定値） | 検証・reject 規則 |
|---|---|---|---|
| `invoke` | `event_config`（or `event_config_ref`）, `reward_table`, `change_request`, `notice_text`, `tenant_id`, `org_id` | `context`（既定 `{}`・空なら注入せず既定挙動）| 4 資料のいずれか欠落で reject。未知キー・型不一致で reject。`event_config` が `max_input_bytes`(256KiB) 超なら **`event_config_ref` 必須**（下 §payload_ref） |
| `ingest` | `records`（過去裁定の配列）, `tenant_id`, `org_id` | — | 各 record の必須フィールド（下 §KB record）欠落・未知キー・型不一致で reject。**書込前サニタイズ必須**（下 (a)） |
| `feedback` | `case_id`（対象 `/invoke` の識別子）, `manager_action`（`corrected`/`accepted`/`rejected`）, `final_verdict`, `tenant_id`, `org_id` | `correction_note`（既定 `""`）| `manager_action` は enum・それ以外 reject。`case_id` が当 tenant/org の既存 case に解決できなければ reject。**書込前サニタイズ必須** |

> **payload_ref（大入力の間接化・checkpoint 隔離＝層1）**: caller は `event_config` を **inline 本文**または
> **`event_config_ref`（事前ステージ済み read-only export への不透明ハンドル）**のいずれか一方で渡す（排他・両方同時は reject）。
> pre_process の決定論的分岐（境界で 1 回評価・pre_process は本文をオフロードしない）:
> - inline 本文かつ `len(event_config) <= max_input_bytes`（256KiB）→ 本文を `validated_input` に格納・`payload_ref = ""`（未使用）。
> - inline 本文かつ `> max_input_bytes` → **reject**（caller が事前ステージして `event_config_ref` を渡し直す）。
> - `event_config_ref` あり → 解決して `payload_ref` に検証済みハンドルを格納。解決失敗・型不一致は reject（fail-closed）。
> ハンドルはリクエスト内・ホスト間で不変（TZ・時刻・乱数に依存しない）。

## 3. Architecture Overview

### Node Configuration（外側 backbone）

| Node | Responsibility | Input State | Output State（部分 dict） | Inherits/Overrides |
|------|---------------|-------------|--------------|-------------------|
| initialize | 既定（InvocationContext 展開・State 初期化） | — | — | InitializeNode (default) |
| pre_process | mode 正準化・**RBAC 認可（tenant/org fail-closed）**・mode 別 closed schema 検証・payload_ref 間接化・S-2 拡張 | user_input, mode, tenant_id, org_id | validated_input, mode, payload_ref, status | `PreProcessNode(FunctionNode)` |
| main | mode 別の内側 domain workflow を実行（GraphNode で内側グラフを包む） | validated_input, mode, payload_ref | **invoke**: source_registry, gap_findings, exception_packet, status ／ **ingest·feedback**: write_result, status | `PromiseReviewGraphNode(GraphNode)` |
| post_process | 出力整形・**provenance 検証（invoke・execute 内）**・**法的断定 lexicon（S-3 hook）**・S-4 監査 | invoke: source_registry, gap_findings, exception_packet ／ write: write_result | result, status | `PostProcessNode(FunctionNode)` |
| finalize | 既定（最終出力の正規化） | — | — | FinalizeNode (default) |

> `add_edges()` は override しない（固定 backbone）。`source_registry` の**正準生産者は内側 reconcile（下記）**であり、
> `merge_output` が外側 State へ surface する（pre_process は生産しない）。post_process はこの surface 済み値を読む。

### Inner domain workflow（`PromiseReviewWorkflowGraph` — main の内側）

**`/invoke` 経路**:

| # | Inner Node | 種別 | 責務 |
|---|---|---|---|
| 1 | `validate_inputs` | 決定論 | §2-1 invoke schema の再検証・欠落判定 |
| 2 | `deterministic_reconcile` | 決定論（内部 Tool・**非 LLM・非 caller 制御**） | ID・報酬テーブル・version の突合、承認欠落検出。**`source_registry` を生産**（`source_id → {kind, version, span, text, facts}` の immutable 集合＝provenance の anchor）。matched_id / signal_id を含む |
| 3 | `promise_normalize` | **LLM** | 告知文が約束する内容を構造化正規化: audience / probabilistic（抽選/確定）/ caps / deadlines / distribution_timing |
| 4 | `actual_rule_reconstruct` | 決定論 | versioned config から実際の報酬実行条件を再構成 |
| 5 | `gap_judgment` | **LLM** | 「どの告知解釈のプレイヤー群が、どの実ルールで期待を裏切られ得るか」を**反実仮想レビュー質問**として起草。各 finding に **citation object**（下）を必須付与。**source/version/span-bound・非断定（レビュー質問形）** |
| 6 | `kb_recall` | 決定論＋検索 | 過去の類似告知修正裁定を KB から想起（`history_ref` 名前空間・`source_id` とは分離・参考ブロック） |
| 7 | `assemble_packet` | 決定論 | source-linked exception packet（gap_findings ＋ 欠落承認 ＋ 過去裁定参考ブロック ＋ レビュー質問）を組み立て |

**`/ingest` `/feedback` 経路（同一内側グラフの mode 分岐）**: `validate → sanitize_records → kb_write`。
書込経路は read-only の `/invoke` より機微なため、pre_process の RBAC（write 権限）＋ tenant/org fail-closed を経た
ペイロードのみ到達し、全書込を S-4 監査する。

### Data Flow

```
START → initialize → pre_process → main(GraphNode) → {route} → post_process → finalize → END
                                                          ↓ (retry, max 3)
                                                       pre_process

  main 内側 (invoke):
    validate_inputs → deterministic_reconcile[source_registry] → promise_normalize(LLM)
      → actual_rule_reconstruct → gap_judgment(LLM)[citations] → kb_recall → assemble_packet
  main 内側 (ingest / feedback):
    validate → sanitize_records → kb_write
```

### State Definition（flat TypedDict — `src/schemas/state.py`）

**Outer State**（外側 backbone の 5 ノードが読み書きする AgentState サブタイプ。inner の作業フィールドは含めない）:

| Field | Type | 生産者 | Purpose |
|-------|------|--------|---------|
| `mode` | `str` | caller→pre_process 正準化 | `"invoke"`/`"ingest"`/`"feedback"` |
| `tenant_id` | `str` | caller（pre_process が RBAC 検証） | テナント（顧客）識別。fail-closed 認可の一次キー |
| `org_id` | `str` | caller（pre_process が RBAC 検証） | 同一 tenant 内の組織/タイトル識別。fail-closed の二次キー |
| `user_input` | `str` | caller | 生入力（mode 別ペイロードの JSON 文字列） |
| `validated_input` | `str` | pre_process | 検証・正規化後の入力 |
| `payload_ref` | `str` | pre_process | 大 read-only 入力の間接ハンドル（層1・§2-1）。inline 本文使用時は `""` |
| `source_registry` | `str(JSON)` | inner #2 → **merge_output で surface（invoke）** | provenance anchor（下 (b)）。immutable。post_process が読む |
| `gap_findings` | `str(JSON)` | inner #5 → **merge_output で surface（invoke）** | `[{gap_id, citations[], version, beneficiary_impact, review_question, uncertainty_text}]`（citation は (b)） |
| `exception_packet` | `str(JSON)` | inner #7 → **merge_output で surface（invoke）** | gaps ＋ 欠落承認 ＋ 過去裁定参考ブロック ＋ レビュー質問 |
| `write_result` | `str(JSON)` | inner kb_write → **merge_output で surface（ingest/feedback）** | 書込結果（`{written_count, record_ids[]}`） |
| `result` | `str(JSON)` | post_process | 最終射影（invoke=exception_packet の非断定射影 / write=write_result の要約） |
| `status` | `str` | 各ノード | `AgentStatus.<X>.value`（"success"/"error"/"retry"） |

**Inner-graph working state**（`PromiseReviewWorkflowGraph` の内側 state。Outer State には surface しない）:
`reconcile_result`（#2 突合結果）/ `source_registry`（#2 生産）/ `promise_params`（#3）/ `actual_rule`（#4）/
`gap_findings`（#5）/ `kb_recall`（#6・`history_ref` 名前空間）/ `exception_packet`（#7）/ `write_result`（kb_write）。
このうち `merge_output` が Outer へ写すのは mode 別に **invoke: source_registry・gap_findings・exception_packet** ／
**ingest·feedback: write_result**（＋ status）のみ。他（reconcile_result/promise_params/actual_rule/kb_recall）は内側に閉じる。

**State Constraints（必須）**: flat TypedDict のみ / JWT・API キー・認証情報を State に置かない /
InvocationContext は `config["configurable"]` 経由のみ（State に入れない・`InvocationContext.from_state(state)` で取得）/
Pydantic・dataclass 不可。

## Framework Utilization

### Shared Components Used
- [x] InvocationContext（correlation_id / session_id / caller_trust_level / caller claims / secrets）
- [x] SecurityViolationError（S-2/S-3・RBAC・provenance 違反時）
- [x] **S-2: `_extra_security_gate_input()`**（pre_process）— ①mode 別 closed schema 検証（§2-1・未知キー/型/未知mode reject）
      ②入力 allowlist（未知形式はマジックバイト判定で拒否＝パーサを持たない選択）③索引に入るテキストへの品質ゲート
      （マジックバイト・制御文字比率・可読文字比率・長さ）④書込 mode（ingest/feedback）ペイロードへ既定 `detect_pii`
      サニタイズ（付随 PII の defense-in-depth。**日本語姓辞書の自前実装はしない**＝正典 §2-4 既定）
- [x] **provenance 検証は post_process の `execute()` 内**で行う（下 (b)）— **理由**: `_extra_security_gate_output(result)`
      は `result` のみ受け取り State を受け取らない。provenance は「LLM 出力（gap_findings）を決定論的 `source_registry`
      と照合」する検証で State（surface 済み source_registry）を要するため、execute() 内で実施し違反は
      `SecurityViolationError`（→ status=error）。これは redact/verify-in-execute パターン（出力ゲート到達前に produce 側で処理する）。
- [x] **S-3: `_extra_security_gate_output()`**（post_process）— **自己完結**の法的断定 lexicon スキャン（下 (c)）。
      `result` の全 string leaf を走査し、禁止断定を検知したら **block（fail-closed）**。State には依存しない。
- [x] **S-4: `emit_trace_event()`** — 各 execute 内で最低 1 ドメインイベント（reconcile / promise_normalize /
      gap_judgment / kb_recall / kb_write / reject / degrade を含む・**成功経路だけに置かない**）

> S-2/S-3 は `FunctionNode` の `@final` ゲートが自動実行。ドメイン拡張は `_extra_*` フックのみ
> （`_security_gate_*` 直定義は import 時 TypeError）。GraphNode（main）の S-2/S-3 は意図的 no-op passthrough
> （内側ノードの `FunctionNode` ゲートが適用済み）。

### Composition Pattern
- **Pattern**: GraphNode（subgraph・in-process）
- **Composition target**: `src/graph/domain_workflow_graph.py` の `PromiseReviewWorkflowGraph`
- **Error propagation strategy**: **propagate**（内側エラーを `SubgraphError` として再送出＝部分結果を返さない。
  レビュー用途では不完全な乖離検出を成果物化すると誤誘導になるため fail-fast）
- **`merge_output`**: 内側 `get_output()` を **mode 別に明示マッピング**して外側 State へ surface（raw pass-through
  しない＝schema 境界の責務）: **invoke** → `source_registry` / `gap_findings` / `exception_packet` / `status`；
  **ingest·feedback** → `write_result` / `status`。`kb_recall` は `exception_packet` の「過去裁定参考ブロック」に
  畳み込み外側 State へ単独 surface しない。
- **post_process の mode 分岐**: invoke → provenance 検証（(b)・execute 内で source_registry×gap_findings を照合）→
  `result` = exception_packet の非断定射影。ingest·feedback → **provenance 検証は不要**（gap_findings 不在）→
  `result` = write_result の要約。いずれも S-3 hook（(c) 法的 lexicon）は result に対して自己完結で走る。
- **`propagate_hitl`**: False（HITL 不採用＝DDR A-3）

## EU AI Act Art.13 Design-Time Evidence

**Not applicable** — EU AI Act Annex III の宣言対象は FIN/GOV/HCR/ENE/RET のみ。
**SVC は対象外**。景表法・ガチャ表示規制への
非断定境界は S-3（(c)）で別途担保する（法令適合の保証ではなく製品安全の緩和策）。

## Import Isolation Confirmation
- [x] Level 0（agenticstar-platform SDK）を import しない
- [x] import 先は `framework/` と `shared/` のみ（`agents/base/` 不要）

## セキュリティ設計（蓄積型・法務隣接の要点）

### (a) 永続 KB の統治（層2）＋ RBAC — 蓄積型の統治契約

**RBAC（識別 → スコープの束縛・fail-closed）**:
- **認可の source**: InvocationContext の caller claims（gateway が JWT/OAuth から検証して付与）。AG は認可を発明せず、
  「リクエストの `(tenant_id, org_id)` が caller の認可済みスコープに含まれるか」を pre_process のハードゲートで強制する。
- **mode 別権限**: `invoke`=当該 `(tenant,org)` の read 権限 / `ingest`・`feedback`=write 権限。
- **reject（fail-closed・KB アクセス前）**: `tenant_id`/`org_id` 欠落・型不一致・**caller 未認可の tenant/org**・
  mode に対する権限不足 → 即 `status = AgentStatus.ERROR.value`。他 tenant/org へ**絶対にフォールバックしない**。
  スコープ引数は**省略可能にしない**（省略した瞬間、呼び忘れが全件検索になる）。tenant_id と org_id は**両方**必須
  （tenant だけだと同一顧客内の別タイトルへ未公開情報が漏れる）。

**KB record schema（typed・書込契約・allowlist）**: 1 裁定レコードのフィールドと型/enum（未知キー・型不一致は書込前 reject）:

| field | type | 分類 | 備考 |
|---|---|---|---|
| `record_id` | `str` | key | 一意 |
| `tenant_id` / `org_id` | `str` | key | RBAC スコープ（両方必須） |
| `notice_summary` | `str` | raw-text | 告知文の要約（保持期間で削除対象） |
| `actual_rule_summary` | `str` | raw-text | 実ルールの要約（削除対象） |
| `gap_note` | `str` | raw-text | 乖離の指摘（削除対象） |
| `correction` | `str` | raw-text | 実際の修正内容（削除対象） |
| `adjudication_rationale` | `str` | structured-retained | 判定理由（観点として保持） |
| `verdict` | `str(enum: corrected/accepted/rejected)` | structured-retained | 裁定結果（moat・保持） |
| `category_key` | `str` | structured-retained | 分類キー |
| `viewpoint_code` | `str` | structured-retained | 観点コード |
| `retrieval_text` | `str` | index (raw 由来) | embedding/全文索引の対象（分類キー＋相談要約＋観点コード）。verdict/`adjudication_rationale` は含めない。相談要約が raw 由来のため retention 対象（(a) 参照） |
| `created_at` | `str(ISO8601+TZ)` | meta | 生成時刻（adapter で 1 回確定） |
| `source_of_truth_ref` | `str` | meta | 顧客側正本レコードへの参照 |

- **プレイヤー PII は core に持たない**（business-key allowlist）。
- **書込前サニタイズを両書込 mode（ingest・feedback）に適用**（既定 `detect_pii`・付随 PII の defense-in-depth）。
  サニタイズ失敗＝1 件も書かない（fail-closed。「できなかったので原文保存」はしない）。
- **retention/削除（必須ルール・executable contract）**: `raw-text` 分類フィールドは保持期間
  `kb_raw_retention_days`（`config/config.yaml`・**既定 365 日は placeholder**／導入時に顧客の保存方針で較正）
  経過で redact/削除する。**executable contract**:
  - (i) **型・検証**＝正の整数（日数）。`<=0`・非整数は **config load 時に reject**（fail-closed。無効値で
    「無期限保持」に黙って倒れない）。
  - (ii) **トリガー・決定論**＝各レコードの `created_at`（adapter で TZ 付き 1 回確定）からの経過が
    `kb_raw_retention_days` を超えた時点。判定の基準時刻も境界で 1 回確定し、リクエスト内・ホスト間で揺れない。
  - (iii) **対象**＝`raw-text`（notice_summary/actual_rule_summary/gap_note/correction）を redact/削除する
    （フィールドを空文字 `""` に置換し実体を消す）。**加えて `retrieval_text`（＝分類キー＋相談要約＋観点コード）は
    相談要約という raw 由来内容を含むため raw-text と同じ retention 対象**とし、期限経過時に (a) その embedding/
    全文索引エントリを索引から除去し (b) `retrieval_text` を **structured-retained のみ（category_key＋viewpoint_code）
    から再導出**して再索引する（＝raw 由来内容を索引に残さない。再導出せず単に索引除去でも可＝その場合そのレコードは
    意味検索の対象外になり record_id 直引きのみ）。`structured-retained`（verdict/`adjudication_rationale`/
    category_key/viewpoint_code）本体は残す（観点の再利用が moat の核）。
  - (iv) **S-4 監査**＝redact/削除ごとに S-4 イベント `kb_raw_text_redacted`（record_id・tenant_id/org_id・
    削除したフィールド名・基準時刻）を必ず出す。
  - (v) **二重の期限強制（露出窓を塞ぐ）**＝(a) 定期 redaction ジョブ（物理削除・cadence は運用パラメータ）に加え、
    (b) **recall/read 時のバリア**＝読み出し・想起の時点で保持期間を過ぎたレコードの raw-text と retrieval_text は
    「不在」として扱い返さない（ジョブ未走査でも raw 由来内容を漏らさない）。ジョブは物理削除、read-time バリアは
    露出窓の即時遮断で、両者は独立に効く。
  - **実装フェーズで実装するもの**: 保持期間経過レコードの raw-text＋retrieval_text/索引 redaction ジョブ ＋ read-time
    バリア ＋ config 値の load 時バリデーション ＋ 上記 S-4 trace ＋ BL/永続層 PB（保持期間内レコードは残存・
    経過レコードの raw-text と retrieval_text/索引 raw 由来分は消える・`structured-retained` は不変・
    read-time バリアがジョブ未走査でも raw を返さない・`kb_raw_text_redacted` イベント発火、を検証）。
- **source_of_truth_ref**: 正本レコード（顧客側の裁定記録）への参照。KB は写しであり正本ではない。
- **過去裁定は「事実」であり漏洩ではない** — **verdict・
  adjudication_rationale を projection から除外しない**（除外すると観点が成立せず蓄積 moat が消える）。防ぐのは
  **今回の結論の断定文調**のみで、それは (c) の非断定契約で担保する。過去実績は**相談内容 / 結論 / 判定理由の
  3 点セット**（notice_summary / verdict / adjudication_rationale）で保持する。
- **検索の軸分離**: embedding/全文索引の対象は `retrieval_text`（分類キー＋相談内容要約＋観点コード）のみ。
  verdict/`adjudication_rationale` は含めず top-k 確定後に record_id で引く（検索順位が結論語彙に引きずられないため。
  「相談内容固定・結論/理由を全件入替で top-k 不変」を実装フェーズで確認）。
- **永続層 PB（実装フェーズ必須）**: PII/credential 非索引化・**tenant A → tenant B / org A → org B の不可視性**。
  State 非格納 PB だけでは永続層を守れない。

### (b) provenance（過去事例想起型 AG に必須）

**citation object**（gap_findings 各 finding の `citations[]` 要素）:
`{source_id, version, span, verbatim_quote, asserted_facts}`。`source_registry[source_id]` が anchor。
- **引用 ID の実在＋version/span 束縛の検証**: post_process の execute() で、gap_findings が引用した全 citation について
  ①`source_id` が `source_registry`（**決定論 reconcile が生産・非 LLM・非 caller 制御**の許可 ID 集合）に実在
  ②`citation.version == source_registry[source_id].version`（version 束縛）③`citation.span` が
  `source_registry[source_id].span` の範囲内（span 束縛）を機械照合。いずれか不成立なら `SecurityViolationError`
  （ID hallucination／version・span なりすまし防止）。
- **ID 実在だけで終わらせない**: ①**逐語引用**（`verbatim_quote` が `source_registry[source_id].text` の literal な
  部分文字列か・**長さ下限 `provenance_min_quote_len`(=8) 必須**。空文字/1 文字は `q in text` が常に真で照合が vacuous）
  ②**claim→source の数値/属性照合**（`asserted_facts` が `source_registry[source_id].facts` と一致するか。LLM が入力
  数値を書き換えても ID 照合だけでは発火しないため）③**双方向**（claims→source の健全性 ＋ source→claims の網羅性。
  ②だけだと「無関係な claim を1件入れ数値は散文だけに書く」で回避される）④抽出は **NFKC ＋ 漢数字変換 ＋
  日本語単位同義語**（`６０分`/`60ぷん` で素通りしない）⑤`kind` は allowlist（未知 kind で「空でない」と主張させない）。
- **過去事例は本件の証拠にできない**: `kb_recall` の識別子は `history_ref`（`source_id` と名前空間が重ならない）で
  `source_registry` に入れない。S-3/provenance が完全一致を要求するため citation の `source_id` に混ぜると reject される。
  prompt 上も「本件（根拠可）」と「過去事例（参考・引用禁止）」を 2 ブロックに分ける。
- **実装フェーズの PB**: 正常 citation ＋ ①短い/非 literal 引用 ②不在 ID ③**実在するが無関係な source_id** ④数値不一致
  （双方向）を必ず含める。

### (c) 法的断定境界（景表法・ガチャ表示規制の隣接）— S-3 acceptance contract

レビュー QA Finding 3 対応の設計必須要件。位置づけ = **製品安全／非権威的出力の緩和策**であり、
景表法・ガチャ表示規制の**遵守を確立するものではない**。

- **許可出力フィールド**: gap possibility（source/version/span-bound）・欠落承認・受益者影響・**レビュー質問形**
  （review_question）・**必須の不確実性テキスト**（uncertainty_text）・過去裁定参考ブロック。
- **禁止する結論形（出力スキーマから排除）**: 「違法/適法」「景表法違反/優良誤認である」「消費者保護上問題ない/ある」
  「掲載可/補償可」等の可否・違反の断定。
- **断定 lexicon は closed set・双方向**: ポジティブ断定（「掲載してよい」「問題ない」「クリア」「適法」）＋
  否定・義務方向（「違反」「優良誤認」「該当する」「要」「必須」「違法」）の**両方**を列挙した closed lexicon。
  正規化（NFKC＋表記ゆれ）後に **result の全 user-visible string field** を走査対象とする。
- **transform と reject の分担**: gap_judgment（execute 内）が出力を**レビュー質問形に整形（transform）**する
  （「合理的根拠の確認対象」「PR 表記要否の確認対象」等）。post_process の `_extra_security_gate_output()`（S-3 hook）は
  整形後の result を走査し、禁止断定が残っていれば **block（reject・fail-closed）** する（gate は transform せず遮断する）。
- **各規則の当てはめは source（条項/config 箇所）/ version / span 付きのレビュー質問**として提示（AG は法的評価を
  確定しない）。基準の妥当性と出力ポリシーの検証は**顧客の法務/コンプラ部門**（`schema_owner`）が行うと明記。
- **最終判定（違法/適法・補償可否・掲載可否）は AG が今回について出力しない**（人間レビュー工程が下流に存在）。
- **止まらない範囲を正直に書く**: 「過去の裁定を読んだ LLM が本件の正しい source_id を添えて結論的自由文を書く」
  経路は S-3 では止まらない（S-3 は「その自由文が証拠から導かれるか」までは検証しない）。したがって
  **「構造的に止める」とは主張しない** — 残余は人間レビュー（live-ops マネージャー＋顧客法務）が担う。

### (d) trust level
- 全 `FunctionNode` に `required_trust_level` を明示（暗黙 ANONYMOUS 継承は不可）。有効値は
  ANONYMOUS / VERIFIED_EXTERNAL / INTERNAL の 3 値のみ。
- read-only `/invoke` および書込 `/ingest` `/feedback` の最小 = **`VERIFIED_EXTERNAL`**（顧客 live-ops 担当）。
  認可の実体は (a) の RBAC（tenant/org fail-closed）で担保する。

## Design Decision Record

| Decision | Option A | Option B | Chosen | Rationale |
|----------|----------|----------|--------|-----------|
| L1 base type | AgentBaseGraph | AutonomousBaseGraph | **AgentBaseGraph** | 固定パイプライン（think-act 自律ループ不要）。乖離検出は決定論＋2 回の LLM で完結 |
| Composition pattern | Standalone | GraphNode（subgraph） | **GraphNode** | 多段ドメインワークフローを内側グラフに隠蔽（Cat 2 標準） |
| error_strategy | propagate | handle | **propagate** | レビュー用途で不完全な乖離検出を成果物化しない（fail-fast） |
| HITL（A-3） | 採用（D6 interrupt） | **不採用** | **不採用** | **グラフ内 suspend の必然性が無い**。出力（exception packet）を live-ops マネージャーが**下流で**運用レビューし、その結果を `/feedback`（別 invocation）で記録する。グラフ実行の途中で人間入力を待って resume する構造ではない |
| provenance 検証の実施箇所 | S-3 `_extra` hook | **post_process execute()** | **execute()** | 検証に surface 済み `source_registry`（State）を要し、`_extra_security_gate_output(result)` は State を受け取らないため。verify-in-execute |
| KB の裁定結果保持（A-4） | verdict を projection から除外 | **verdict を保持** | **保持** | 過去裁定は組織の事実（漏洩ではない）。除外すると観点が成立せず蓄積 moat が消える。断定抑制は (c) で担保 |
| 二層データ統治の適用範囲 | 過去結論も隔離 | **PII/規制データのみ隔離** | **PII/規制データのみ** | core にプレイヤー PII を持たない（business-key allowlist）。過去裁定は隔離対象でない |
| KB raw-text 保持の既定値（A-4） | 値未定義（顧客較正のみ） | **config 既定 365 日＋較正注記＋load 時検証** | **365 日（placeholder）** | executable contract を持たせつつ導入時較正を許す（設計レビュー medium 対応）。値は運用パラメータで AI 裁量（正典 §2-3）・0/非整数は fail-closed reject |

### 設計段階の自己点検

| 先回り項目 | 該当 | 本設計での対処 |
|---|---|---|
| ① provenance を「ID 実在」で終わらせない（逐語引用・数値双方向照合・NFKC/漢数字/単位・kind allowlist・無関係 source_id の PB） | **該当** | (b) に citation object＋全項目を設計要件化。実装フェーズの PB に「実在するが無関係な source_id」を必須 |
| ② 永続 KB を持つなら統治契約を design 入場条件に（許可/禁止フィールド・tenant_id と org_id 両方 fail-closed・スコープ省略不可・保持/削除・S-4 監査・永続層 PB） | **該当** | (a) に RBAC＋KB record schema＋retention＋両書込 mode サニタイズ＋永続層 PB を明記 |
| ③ PoC 検証計画を proposal に | **非該当（条件化しない）** | 販売実現性の未検証は提案側に正直に記録し、受入条件にはしない（顧客具体化は営業マターとして分離） |
| ④ §1「継承先」欄は継承クラスのみ（GraphNode/inner graph は構成） | **該当** | 継承欄＝`AgentBaseGraph` のみ。GraphNode/inner は Composition Pattern 節で扱う |
| ⑤ 外部ソース確認は実施日を明記 | **非該当** | 本 AG は**外部 API 依存なし**（全入力 customer-held）。確認対象の外部データ源が無い |
| ⑥ 入力の allowlist ＋ 品質ゲート（マジックバイト拒否・索引テキストの品質ゲート） | **該当** | (a)/Framework Utilization S-2 に mode 別 closed schema・allowlist・索引テキスト品質ゲートを明記 |
| 追加: 法的断定境界（景表法/ガチャ）の acceptance contract | **該当** | (c) に許可フィールド/closed 双方向 lexicon/transform・reject 分担/schema_owner を明記（レビュー Finding 3 対応） |

### 「宣言 vs executable contract」自己点検（High 指摘の原因分析から得た 5 問）

本設計で「任意/できる/省略可」と書いた箇所と、その executable contract:

| 箇所 | ①欠落時の既定値と allowlist | ②未知キー/型 reject | ③決定論 | ④write の RBAC | ⑤証明する実装テスト |
|---|---|---|---|---|---|
| `context`（invoke 任意） | 既定 `{}`・注入せず既定挙動 | §2-1 closed schema で reject | 境界で 1 回確定 | read（invoke） | 有/無 context の invoke 成功 ＋ 未知キー context の reject |
| `payload_ref`（大入力） | 閾値以下なら `""`（未使用）・超過なら `event_config_ref` 必須 | ref 解決失敗/型不一致 reject | `len<=max_input_bytes` を境界で 1 回評価・ハンドル不変 | read（invoke） | no-ref 成功 ＋ valid-ref 成功 ＋ oversize/malformed reject |
| `ingest`/`feedback` 書込 | 必須キー欠落は reject | closed schema で reject | — | **write（VERIFIED_EXTERNAL＋tenant/org fail-closed）** | 認可済み書込成功 ＋ 未認可 tenant/org reject ＋ 書込 PII サニタイズ ＋ tenant 分離 PB |
| `correction_note`（feedback 任意） | 既定 `""` | closed schema で reject | — | write | 有/無 note の feedback 成功 |
| `kb_raw_retention_days`（保持期間・config） | 既定 365 日（placeholder・導入時較正） | 非正/非整数は config load 時 reject | 基準時刻を境界で 1 回確定（TZ 付 `created_at` 経過）。ジョブ＋read-time バリアの二重強制 | — | 保持期間内レコード残存 ＋ 経過 raw-text／retrieval_text・索引 raw 由来分 削除 ＋ read-time バリアがジョブ未走査でも raw を返さない ＋ `structured-retained` 不変 ＋ S-4 `kb_raw_text_redacted` 発火 |

### 実装確定差分（DDR に畳み込み）

実装で確定した設計上の決定（設計⇄実装を同じ改版で整合）:

| 論点 | 実装で確定した内容 | 根拠 |
|---|---|---|
| RBAC の認可 source | framework `InvocationContext` に claims dict は無い（`caller_id`/`caller_trust_level` のみ）。認可済みスコープは **entry-point の trusted channel = `input_context.authorized_scopes`** で運ぶが、その解決は **client が偽装できない 2 チャネルに限定**（B-6 解決・下表参照）: `request.state`（middleware 注入）または **HMAC 署名済 `X-Authorized-Scopes`**。生ヘッダは不信＝fail-closed。body（`user_input`）は**要求**スコープのみを運び、pre_process が「要求 ⊆ 認可」を強制。mode 別 permission（invoke=read / ingest·feedback=write。permission 未指定スコープは read-only 既定＝write は明示付与必須） | framework 実体確認（`invocation_context.py`）。§(a) mode 別権限。B-6 信頼境界 |
| `result` の型 | State table の `str(JSON)` は**JSON-serializable dict** として実装（134 と同じ・S-3 lexicon が string leaf を走査可・msgpack 安全）。事前シリアライズ文字列にはしない | 134 `formatted_output` パターン踏襲・gate 走査容易 |
| 内側 3 スロットへの写像 | 決定論ステップ（validate/reconcile #2/reconstruct #4）を **inner pre にまとめ**、inner main は LLM 2 ステップ（promise #3 / gap #5）専任、inner post が recall #6 + assemble #7。これで大 `event_config` は inner pre の local scope に閉じ **State に一切載らない**（層1強化） | §Inner domain workflow・層1 checkpoint 隔離 |
| payload_ref の解決経路 | `src/services/export_store.py` の注入シーム（`(tenant,org,ref)` スコープ束縛・fail-closed）を inner pre が local scope で解決。resolved config は State に書かない | §2-1 payload_ref |
| S-3 lexicon の走査範囲 | 断定 lexicon は **AG が本件で書いた自由文のみ**を走査。`past_reference`（過去裁定の記録事実）と `verbatim_quote`/`citations`（出所証拠）は path-skip。過去裁定 verdict はキーを `past_verdict`/`past_rationale` に名前空間化し current-case `verdict` floor と衝突させない | 過去裁定は事実であり漏洩ではない（前提を過剰に置くと蓄積価値そのものを潰す） |
| kb_recall と gap_judgment の関係 | 設計順（#5→#6）どおり kb_recall は gap_judgment に**渡さない**（downstream の人間向け参考ブロック専用）。これで「recall 自由文 → 次 LLM prompt」の injection 経路が構造的に消える | KB 自由文は raw / retrieval / 参考表示の 3 値に分離する |

### 敵対的レビュー対処（R1）

push 前の敵対的レビューで検出し、実装で fail-closed 化した項目（各々に回帰テスト）:

| # | 指摘 | 対処 | テスト |
|---|---|---|---|
| H-1 | payload_ref 経路でも `source_registry.text` に巨大 export が入り State に載る | `build_source_registry` を **per-source 文字数上限＋source 数上限**で bound（`reconcile._add`・`max_source_chars=max_field_chars`）。超過引用は照合失敗＝fail-closed | `test_reconcile::test_source_text_is_capped` / `test_source_count_is_capped` |
| H-2 | `reward_table`/`change_request`/`context` が size 無制限で State へ | invoke schema で各フィールドを `max_input_bytes` で bound | `test_input_gate::test_oversize_reward_table_rejected` |
| H-3 | 悪意ある finding が `gap_id` に dict を仕込み S-3 を回避 | provenance に**構造型検証**（gap_id は非空 str・自由文フィールドは str）を追加＝projection 前に reject | `test_provenance::test_malformed_gap_id_rejected` / `test_non_string_free_text_rejected` |
| H-4 | `get_record()` の既定引数で read-time バリアを迂回 | `now_iso`/`retention_days` を**必須化**しバリア常時適用（迂回パス排除） | `test_kb_store::test_read_time_barrier_excludes_expired`（recall）＋物理検査は `_records` 直参照へ |
| M-1 | lexicon が `違法性はない` 等の平易な断定を取りこぼし | 双方向 phrase を追加＋`eligibility_decision` 等を floor へ（**設計 (c) どおり lexicon は二次網・完全保証ではない／残余は人間レビュー**） | `test_lexicon::test_added_phrase_blocked` / `test_eligibility_decision_key_blocked` |
| M-2 | 数値ガードが `1万`/`一万` を `1` と誤認（unbacked magnitude 素通り） | `numerics` に **万/億 magnitude** を追加（arabic+万・漢数字とも） | `test_numerics::test_man_oku_magnitude` / `test_provenance::test_man_magnitude_bidirectional_guard` |
| M-3 | `source_of_truth_ref` が未サニタイズで無期限保持 | 書込サニタイズ対象に追加（PII マスク＋命令文 reject・fail-closed） | `test_graph_invoke::test_ingest_source_of_truth_ref_sanitized` |
| L-1 | 上流 error 短絡でドメイン S-4 event が出ない | inner main/post の短絡に `upstream_error_passthrough` を emit | PB-1 系 |
| B-6（解決・実装フェーズのレビュー） | **standalone `X-Authorized-Scopes` を生ヘッダのまま信頼**していた（gateway strip を文書前提にするだけ＝アプリで未強制）。gateway 外に露出すると scope 偽装で tenant/org 認可を突破できる（High blocker） | server.py の `_trusted_scopes()` で **client が偽装できない 2 チャネルからのみ**スコープを解決し fail-closed 化: ①`request.state.authorized_scopes`（in-process AuthMiddleware 注入・remote client は設定不可）②`X-Authorized-Scopes` の **HMAC-SHA256 署名検証**（`X-Authorized-Scopes-Signature` × 共有 `SCOPE_SIGNING_KEY`）。**生の未署名ヘッダは一切信頼しない → `[]` ⇒ RBAC が全 tenant/org を拒否**。JWT 再検証はしない（gateway 責務・trust model 非分岐＝チャネル完全性のみ検証）。`SCOPE_SIGNING_KEY` は InvocationContext 生成前に読む deployment-level channel credential（entry-point 例外・`INVOKE_AUTH_TOKEN` と同類・`requires.secrets`/state 非搭載） | `test_server_rbac_boundary`（偽装ヘッダ→error／署名済 or 注入→success／`_trusted_scopes` 単体） |

## 参考（gate が grep する必須文字列・改変しないこと）
- `L1 Base`（設計ゲートが docs/02 を検査する項目）: 上「Position」節に存在。
