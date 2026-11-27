# Content Unit Canonicalization & Locator Contract (`M1-T103A`)

## 1. Frozen Version Identifiers
- `CANONICALIZATION_VERSION = "cm-canon-nfc-lf-v1"`
- `PARSER_VERSION = "cm-unit-parser-v1"`
- `LOCATOR_SPEC_VERSION = "cm-locator-v1"`
- `RETENTION_CLASS_NONE = "NONE"`

## 2. Canonical Normalization Pipeline (`cm-canon-nfc-lf-v1`)
Given raw wire bytes (`bytes`), `canonicalize_source_bytes` executes in exact deterministic order:
1. **MIME gate**: Reject non-text MIME types with `UNSUPPORTED_MIME`.
2. **Generated / Minified path gate** (single-file inputs): Reject lockfiles, build/vendor dirs, generated protobuf/codegen files (`GENERATED_ARTIFACT`), and `*.min.js`/`*.min.css` (`MINIFIED_ASSET`).
3. **Byte budget gate (pre-decode)**: Enforce `max_raw_bytes` (`OVERSIZED_INPUT`) or `max_diff_bytes` (`DIFF_BOMB_LIMIT`) before decoding to prevent memory exhaustion.
4. **Binary gate**: Reject known binary magic signatures (PNG, JPEG, GIF, PDF, ZIP, GZIP, ELF, WASM, SQLite, Mach-O) and any NUL (`\x00`) byte with `BINARY_CONTENT`.
5. **Strict UTF-8 decode**: Decode with `errors="strict"`. Invalid UTF-8 sequences return `INVALID_ENCODING` (`errors="replace"` is strictly forbidden).
6. **Control-byte ratio gate**: Reject decoded text whose non-whitespace control-character ratio exceeds `max_control_byte_ratio` (`BINARY_CONTENT`).
7. **Text normalization**:
   - Strip **at most one** leading UTF-8 BOM (`\ufeff`), recording `bom_stripped` and `raw_bom_bytes` (preserving original raw byte provenance via `raw_sha256` / `raw_byte_length`).
   - Normalize `\r\n` and standalone `\r` to `\n`.
   - Normalize Unicode to `NFC` (`unicodedata.normalize("NFC", ...)`).
   - **Zero whitespace mutation**: Indentation, tabs, spaces, and trailing newlines are preserved verbatim.
8. **Content minified & line-length gates** (single-file inputs; unified diffs enforce per file/hunk): Detect minified bundles (`MINIFIED_ASSET`) and lines exceeding `max_line_bytes` (`LINE_LENGTH_EXCEEDED`).

## 3. Span & Offset Contract (`CLAIM-QA` `0.4.0-synthetic-golden` Compatible)
All offsets operate on canonicalized `cm-canon-nfc-lf-v1` text with half-open `[start, end)` semantics:
- `char_start`, `char_end`, `char_length`: 0-based Unicode code point indices (Python `str` slicing; 100% compatible with `CLAIM-QA` `fixture-nfc-lf-v0-draft`).
- `utf8_byte_start`, `utf8_byte_end`, `utf8_byte_length`: 0-based UTF-8 byte offsets over `normalized_text.encode("utf-8")`.
- `utf16_start`, `utf16_end`: 0-based UTF-16LE code unit offsets (surrogate-pair safe for emoji/SMP characters).
- `line_start`, `line_end`: 1-based line numbers inclusive of the first and last character in the span.
- `span_sha256`: `"sha256:" + hex(SHA-256(span_text.encode("utf-8")))`.

## 4. Official `ContentUnitType` Taxonomy (8 Types)
`PR_TITLE`, `PR_BODY_SECTION`, `ISSUE_SECTION`, `COMMENT`, `REVIEW_COMMENT`, `COMMIT_MESSAGE`, `DIFF_HUNK`, `SYMBOL_CHANGE`.
- Fixed-token chunking is forbidden.
- Markdown parsers (`PR_BODY_SECTION`, `ISSUE_SECTION`) respect CommonMark fenced code blocks (`` ``` `` and `~~~`) so `#` comments inside code fences never create false section splits.
- Unified diff parsers (`DIFF_HUNK`) split per file and `@@ -a,b +c,d @@` hunk, track `previous_file_path -> file_path` renames, and strip trailing raw code context from persisted `hunk_header`.
- Outdated/unmatched `REVIEW_COMMENT` anchors fail closed to `AnchorResolutionState.OUTDATED_UNRESOLVED` with `support_eligible = False`; renames remain `RENAMED_TRACKED` only when file rename + line overlap + optional `anchored_hunk_sha256` match a current diff hunk.

## 5. Explicit Skip Reason Taxonomy (Zero Silent Drops)
`SkipReasonCode`:
- `BINARY_CONTENT`
- `INVALID_ENCODING`
- `OVERSIZED_INPUT`
- `DIFF_BOMB_LIMIT`
- `LINE_LENGTH_EXCEEDED`
- `GENERATED_ARTIFACT`
- `MINIFIED_ASSET`
- `UNSUPPORTED_MIME`

Every skip emits a `DeterministicSkipRecord` with `processed_range: ProcessedRange` (`raw_bytes_processed/total`, `chars_processed/total`, `lines_processed/total`, `files_processed/total`, `hunks_processed/total`, `skipped_from_char/to_char`, `skipped_from_byte/to_byte`), `observed_bytes`, `budget_limit`, `is_partial`, and machine-readable `detail_code` (never raw text).

## 6. Option C Zero-Raw-Text Persistence & SEC-02 Isolation
- Ephemeral in-memory wrappers (`EphemeralCanonicalDocument`, `EphemeralResolvedUnit`, `EphemeralVerifiedSpan`, `UntrustedDataSlot`, `UntrustedLLMDataEnvelope`) redact raw text in `__repr__` and `__str__`.
- Persisted metadata (`PersistedContentUnitMetadata.to_persisted_anchor_dict()`) enforces `text_content=None`, `content_ref=None`, `retention_class="NONE"`, and excludes all raw snippet keys (`quote`, `raw_body`, `extracted_span`, `raw_text`, `snippet`, `untrusted_text`, `body_text`).
- `build_untrusted_llm_data_envelope` / `UntrustedSourceContextBuilder` isolate untrusted external text strictly inside JSON-encoded `user` data slots bound by collision-free `boundary_nonce` and `span_sha256`, separate from `system` instructions.

## 7. Downstream Entrypoints (`M1-T103B` Resolver & `M1-T106` Schema/CI)
Import from `career_memory_api.canonicalization`:
- `canonicalize_source_bytes(raw_bytes, *, file_path=None, mime_type=None, is_diff=False, budget=None) -> CanonicalizationResult`
- `parse_content_units(document, source_kind, *, budget=None, comment_context=None, commit_context=None, review_comment_context=None, symbol_change_context=None, current_diff_units=None) -> ContentUnitParseResult`
- `extract_verified_span(document, locator, *, expected_source_content_sha256=None) -> SpanVerificationResult`
- `build_span_locator(normalized_text, char_start, char_end) -> SpanLocator`
- `build_untrusted_llm_data_envelope(system_instructions, units) -> UntrustedLLMDataEnvelope`
- `render_untrusted_text_safe(raw_text) -> str`
- `redact_for_logging(message) -> str`
