# Data Sources

The private runtime can use multiple public and licensed research providers.
The public system mirror contains adapters and schemas only; it does not
publish provider payloads or purchased data.

- BaoStock and AKShare: research adapters for A-share daily and auxiliary data.
- FreeStockDB: optional local third source and minute-data adapter.
- X and Zhihu: public-source adapters requiring the local user's own session.
- Board relative-strength records: derived from the configured board provider.

Provider availability, permissions, rate limits, adjustments, and licensing
remain the operator's responsibility. The public dataset contains derived
audit facts and explicit data-quality status, not raw vendor responses.
