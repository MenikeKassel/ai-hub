# TinyShare / TinyShare Proxy Source Assessment

Checked: 2026-08-02

## Executive decision

`tinyshare` should **not** be installed in the KOL audit workbench environment or
accepted as a persistent market-data provider. The package and service do not
currently meet the project's provenance, transport-security, reproducibility,
licensing, or auditability requirements.

The separately purchased daily-data archive under
`D:\a_data\数据更新时间2026.7.31` is potentially useful as a read-only historical
stock-daily source. It should be audited and imported independently of
`tinyshare`; the archive does not justify trusting the proxy SDK.

The authorization value shared in chat must be treated as exposed. Do not place
it in code, `.env`, logs, Git, Hermes, or the KOL database. Revoke or replace it
through the seller if that is still possible.

## Scope and method

- No package was installed or executed.
- No authorization value was used, transmitted, or copied into this report.
- No authenticated API request was made.
- The vendor wheel was downloaded only for static archive inspection. Its core
  modules were not decompiled; only package metadata, the clear-text loader,
  archive contents, and literal strings were inspected.
- Sources were limited to the package indexes/artifact, the vendor's own docs,
  pip's official docs, and Tushare's official docs and terms.

## Provenance findings

### 1. The public PyPI project is quarantined

PyPI's current Simple API marks `tinyshare` as `quarantined` and lists no
download files. The Simple API does not state the reason, so this is not proof
of malware by itself, but it is a blocking supply-chain warning.

Source: [PyPI Simple index for tinyshare](https://pypi.org/simple/tinyshare/)

### 2. The install command uses a separate, privately controlled index

The vendor index currently serves `tinyshare-0.1036.0-py3-none-any.whl`. Its
index link does not publish a hash fragment. The vendor documentation is branded
`minishare`, recommends installing `minishare`, and separately serves
`minishare-0.1009.0`; this is inconsistent with the sold instruction to install
and import `tinyshare`.

Sources: [tinyshare vendor index](https://minidoc.pages.dev/simple/tinyshare/),
[minishare vendor index](https://minidoc.pages.dev/simple/minishare/),
[vendor documentation](https://minidoc.pages.dev/)

The inspected `tinyshare` wheel had SHA-256
`97c79cd79575fc028ac115f777084ccf59b83daddbdefd6c861737aaf448ea4c`.
This is an observation from the downloaded artifact, not a vendor-published
integrity guarantee.

The suggested `--extra-index-url` form is also a known dependency-confusion
risk: pip searches all configured indexes and selects the best matching
candidate without giving the extra index lower priority.

Source: [pip install documentation and warning](https://pip.pypa.io/en/stable/cli/pip_install/#cmdoption-extra-index-url)

### 3. Ownership and source transparency are inadequate

The wheel declares itself alpha, proprietary, and owned by "TinyShare Team".
Its homepage is the placeholder `github.com/yourusername/tinyshare`; the wheel
does not identify a legal entity or verifiable source repository. The package's
functional implementation is distributed as Python-version-specific bytecode,
while the visible source is primarily a loader that executes that bytecode.
This prevents a normal source review and reproducible build comparison.

Source artifact: [vendor tinyshare wheel](https://minidoc.pages.dev/packages/tinyshare/tinyshare-0.1036.0-py3-none-any.whl)

### 4. The SDK sends requests to vendor infrastructure, not Tushare directly

Static artifact inspection found vendor endpoints under `mintree.site`, API
routes named `api/tushare` / `api/tspure`, an authorization-code field, and a
generated device identifier. The package contains logic referencing hardware
and operating-system identifiers and a local device-ID cache. The configured SDK
endpoints are plain HTTP. The bytecode also contains request options and warning
suppression symbols consistent with disabled TLS verification, although the
closed bytecode prevents full control-flow verification.

The vendor web documentation likewise declares a direct HTTP API base and says
the browser site uses a same-origin proxy. Therefore the advertised import swap
is an API facade over vendor servers, not the official Tushare SDK talking to
official Tushare infrastructure.

Source: [vendor documentation](https://minidoc.pages.dev/), plus static
inspection of the wheel linked above.

## Compatibility assessment

The wheel exposes familiar names such as `set_token`, `pro_api`, dynamic API
method dispatch, and `pro_bar`. That can make basic examples look source
compatible. It does **not** establish full compatibility:

- there is no published compatibility matrix against Tushare SDK versions;
- there is no public conformance test suite;
- response provenance and update timing are not documented per proxied API;
- the vendor docs emphasize a separate collection of minishare APIs rather
  than complete Tushare coverage;
- errors, limits, field additions, revision semantics, and adjusted-price logic
  may differ even when a method name matches.

Tushare documents `pro_bar` as SDK-side integrated logic, not a normal HTTP API,
which makes a third-party drop-in implementation especially difficult to verify.

Source: [Tushare pro_bar documentation](https://tushare.pro/document/1?doc_id=109)

Conclusion: treat compatibility as **unverified and partial**, not as a safe
one-line replacement.

## Contract and credential risk

Tushare's service terms say tokens must be protected, service rights are
personal and non-transferable, and users must not obtain access through
non-official channels. The terms also say Tushare has not authorized third
parties to sell or transfer service eligibility. Unless the seller can provide
verifiable written authorization from Tushare, a one-day "5000-point proxy"
appears inconsistent with those terms.

Source: [Tushare data service agreement](https://tushare.pro/document/1?doc_id=405)

The vendor authorization code is a credential for the proxy service and will be
sent, together with a device identifier, to vendor infrastructure. It should not
be confused with a direct Tushare token or trusted merely because the method
names resemble Tushare.

## What one day of 5000-point access can and cannot provide

If access is obtained through an **official Tushare account and official SDK**,
5000 points can be valuable for a one-time, immutable snapshot:

| Priority | Dataset | Project value |
| --- | --- | --- |
| High | `stock_basic`, `trade_cal` | Security master, listings/delistings, exchange and calendar |
| High | `daily_basic` | Turnover, volume ratio, PE/PB, shares and market value |
| High | `adj_factor` | Independent corporate-action and adjustment audit |
| High | `income_vip`, `balancesheet_vip`, `cashflow_vip`, `fina_indicator_vip` | Quarter-wide financial snapshots for event dossiers |
| Medium | `index_member_all` | Auditable industry membership mapping |
| Experimental | `moneyflow` | Vendor-defined weak feature only; never a trading truth source |

Official documentation confirms that 5000 points gives higher frequency for
ordinary data and enables quarter-wide VIP financial extraction, but does not
grant everything.

Sources: [Tushare point/frequency table](https://tushare.pro/document/1?doc_id=290),
[stock_basic](https://tushare.pro/document/1?doc_id=25),
[daily_basic](https://tushare.pro/document/2?doc_id=32),
[adj_factor](https://tushare.pro/document/2?doc_id=28),
[fina_indicator](https://tushare.pro/document/2?doc_id=79),
[cashflow](https://tushare.pro/document/2?doc_id=44),
[industry membership](https://tushare.pro/document/2?doc_id=335), and
[moneyflow](https://tushare.pro/document/2?doc_id=170).

Important exclusions:

- ordinary A-share daily prices need far fewer than 5000 points;
- historical and real-time minute data are separately authorized;
- announcements, news and several real-time datasets are separately paid;
- 10000+ point characteristic datasets are not included in a 5000-point tier.

Source: [Tushare point/frequency table](https://tushare.pro/document/1?doc_id=290)

Because the purchased access lasts only one day, it is unsuitable as a daily
runtime dependency. At most, an official one-day session should create dated,
immutable raw snapshots with request manifests, row counts, hashes, field
schemas, and cross-source samples.

## Local daily-data archive assessment

Read-only inspection of `D:\a_data\数据更新时间2026.7.31` found:

- 5,875 CSV files, approximately 5.31 GB extracted;
- per-symbol A-share daily history through 2026-07-31;
- unadjusted OHLCV/amount, adjustment factors, and precomputed forward/backward
  adjusted OHLC columns;
- no sampled ETF `159139` file and no sampled CSI 300 `000300` file;
- a separate list of corporate-action stocks for 2026-07-31.

This archive is useful for stock-history backfill and adjustment cross-checking,
but it does not replace the current ETF, index, calendar, instrument-master,
financial, or announcement sources. Before import, it needs license confirmation,
schema and unit checks, duplicate/date-gap tests, delisted-symbol handling,
adjustment verification, and cross-checks against BaoStock and FreeStockDB.

## Recommendation for the KOL audit workbench

1. Do not install `tinyshare` and do not store its authorization code.
2. Revoke/replace the exposed proxy credential.
3. Register `D:\a_data\数据更新时间2026.7.31` as an untrusted, read-only
   `purchased_daily_20260731` source; import only after a manifest and audit pass.
4. Keep BaoStock as the formal daily source and FreeStockDB as the local third
   source/minute supplement. The purchased archive may backfill and cross-check,
   but must not overwrite a passing canonical series.
5. If an official Tushare token becomes available, use the official `tushare`
   package in an isolated one-shot importer. Persist raw responses and manifests,
   then expire the credential and disable the provider after the snapshot.
6. Give priority to fundamental, valuation, corporate-action, security-master,
   and industry-membership snapshots; daily OHLC is already duplicated locally.

Overall rating:

- Purchased CSV archive: **useful after audit**.
- Official one-day Tushare 5000-point snapshot: **useful for targeted enrichment**.
- `tinyshare` proxy/package: **not appropriate for this project**.
