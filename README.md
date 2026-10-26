# IMF Real Exchange Rate Explorer

A Python 3.12 / Streamlit application for FINC 470. Compare two economies using
monthly IMF consumer price indices and period-average exchange rates. Explore
the real index, the actual nominal cross-rate, or both on separate axes. Change
the sample without restarting, inspect coverage, and download every selected
calendar month as CSV.

The application consists of exactly **`app.py`, `requirements.txt`, and
`README.md`**. Self-checks live in `app.py`. No economic data are bundled, generated
for display, or silently sourced from another provider.

## Install and launch

Use Python **3.12**:

```bash
cd real-exchange-rate-app
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
streamlit run app.py
```

On Windows, activate with `.venv\Scripts\activate` instead. Dependencies are
Streamlit, pandas, requests, Plotly, and pycountry. `requirements.txt` includes
compatible version ranges for every direct dependency.

In the prepared cloud environment the virtual environment is **outside** the
application folder:

```bash
cd /workspace/real-exchange-rate-app
source /workspace/.venvs/real-exchange-rate-app/bin/activate
streamlit run app.py --server.headless true --server.address 0.0.0.0
```

Use the existing checkout; cloud tasks are already isolated and do not require a
new worktree. Live server processes need to be restarted in new tasks.

## Using the explorer

1. Search **Country 1** and **Country 2**; the defaults are USA and JPN. Selecting
   the same country twice produces a clear error.
2. Select inclusive **Start month** and **End month**. January 1976 is the earliest
   permitted request. The upper bound comes from the latest usable **observed**
   input for the selected pair, never from a synthetic USD identity. A later
   endpoint date does not imply all four inputs are available through that date.
   When no data arrive, the app explains why no sample bounds can be established.
3. Choose **Real**, **Nominal**, or **Both**. Hover for units and dates; zoom and
   pan with Plotly. Both uses separately labeled axes. Gaps break the lines.
4. Inspect **Monthly data**, **Data coverage**, and **Methodology & source details**.
   Press **Download CSV** to export the current sample, including missing months.

The initial window is the most recent 120 calendar months, or the shorter
available request window. Each edit immediately recalculates the real index.
**Refresh source data** clears the application's caches.

## IMF access, metadata, and series

Primary source: IMF Statistics Department, SDMX **3.0** REST API:

```text
https://api.imf.org/external/sdmx/3.0
```

Official API information: <https://data.imf.org/en/Resource-Pages/IMF-API>.

The application first requests live metadata using:

```text
/structure/dataflow/IMF.STA/CPI/+?references=all
/structure/dataflow/IMF.STA/ER/+?references=all
Accept: application/vnd.sdmx.structure+xml;version=3.0.0
```

The **full `3.0.0` media-type version matters**: live IMF requests rejected `3.0`
and `2.1`. `+` is a latest-version selector. The app resolves each returned
dataflow and DSD to its actual concrete version, follows SDMX version-range URNs
and inherited concept codelists, and checks the dimension count, order, and
codes before using that dataflow. It follows a separate DSD reference if needed.
Unverifiable metadata stops that flow, with an explanation, rather than guessing
an API version or mixing series.

Verified on **2026-10-26** (discovered dynamically, not pinned in code):

| Flow | Dataflow version | Ordered key dimensions | Key |
| --- | --- | --- | --- |
| IMF.STA/CPI | 5.0.0 | COUNTRY, INDEX_TYPE, COICOP_1999, TYPE_OF_TRANSFORMATION, FREQUENCY | `{code}.CPI._T.IX.M` |
| IMF.STA/ER | 4.0.1 | COUNTRY, INDICATOR, TYPE_OF_TRANSFORMATION, FREQUENCY | `{code}.XDC_USD.PA_RT.M` |

CPI is an **index level** (`IX`), not a monthly inflation rate. FX is
**local-currency units per USD, monthly period average** (`XDC_USD`, `PA_RT`, `M`),
not an end-of-period rate. The app displays the actual codelist descriptions,
keys, versions, retrieval times, and URLs in its methodology panel.

Data requests use:

```text
/data/dataflow/IMF.STA/{flow}/{resolved_version}/{key}
Accept: text/csv;version=2.0
c[TIME_PERIOD]=ge:1976-01+le:YYYY-MM
```

The end filter uses the current UTC month; selectors are bounded by returned
observations. Query parameters are URL-encoded by requests, including the `+`
between period conditions. SDMX CSV `YYYY-M01`, ordinary `YYYY-MM`, and
`YYYY-Jan` periods are parsed explicitly. IMF's `SCALE` attribute (CL_UNIT_MULT)
or standard `UNIT_MULT`, when present, is applied as a decimal exponent.
The CSV's returned series dimensions must match the requested key.

### Optional credentials and networking

Public read requests worked **without a subscription key** in this environment.
If IMF access requires a subscription in your deployment, provide the optional
environment variable **`IMF_SDMX_SUBSCRIPTION_KEY`** securely. The app sends it as
`Ocp-Apim-Subscription-Key` in request headers, never in URLs, exports, or logs.
Do not put credentials into source control or send them in chat.

Allow HTTPS access to `api.imf.org`; `data.imf.org` is the documentation host.
Cloud environment settings have a saved draft adding these two hosts while
preserving the package-manager preset. Review/save and publish the environment
configuration to retain the prepared setup for future tasks. A draft save alone
does not publish an environment.

Requests verify TLS and use 6-second connection / 35-second read timeouts.
There are at most two retries for connection failures, HTTP 429, and transient
500/502/503/504 responses, with exponential backoff and a bounded Retry-After.
Successful metadata is cached for 24 hours; discovery errors for 5 minutes;
pair observations and their source errors for 1 hour. Refresh clears all caches,
including after a credential or network change.

### Country universe

The preferred country list is the union of the live CPI and ER reference-area
codelists. Codes are checked against the maintained ISO 3166 country/territory
registry in pycountry, with IMF-specific **UVK**, **WBG**, and explicit **G163**
support (and XKX if present in live metadata). Other regional aggregates are
excluded. Names are human-readable; `G163` is never replaced with an ISO currency
or country code. Codelist membership does not promise a nonempty monthly series.

If metadata is inaccessible, the selectable fallback is the complete roster of
**191 IMF members** as of October 2026 (including Liechtenstein), plus Hong Kong
SAR, Macao SAR, Taiwan Province of China, Puerto Rico, West Bank and Gaza, and
Euro area. This fallback is explicitly labeled in the UI. `MEMBER_CODES` is the
auditable membership list; names come from pycountry with explicit IMF overrides.
The membership reference is <https://www.imf.org/external/np/sec/memdir/memdate.htm>.
Other reporting territories appear when live metadata is available. Fallback
selection does not bypass the requirement to verify a dataflow before loading it.

## Definitions and data integrity

For each month t:

```text
E1(t) = Country 1 local-currency units per USD
E2(t) = Country 2 local-currency units per USD
P1(t) = Country 1 CPI index level
P2(t) = Country 2 CPI index level

S(t) = E2(t) / E1(t)
RER(t) = S(t) * P1(t) / P2(t)
RER_star = arithmetic mean of RER(t) over valid selected months
Real_Index(t) = 100 * RER(t) / RER_star
```

**Direction:** S is Country 2 currency per **one** unit of Country 1 currency.
For E1 = 2, E2 = 100, P1 = 120, P2 = 80: S = 50 and RER = 75.
Swapping the countries makes the nominal cross-rate reciprocal wherever both
FX inputs are positive. The nominal series is never normalized or rebased.

A valid real-rate month requires all four inputs to be present, numeric, finite,
and strictly positive. Every calendar month has equal weight. All raw series
are aligned to month-start timestamps on the selected inclusive calendar.
Missing CPI excludes that month from the real mean but leaves a usable nominal
rate intact. No interpolation, forward filling, extrapolation, or zero filling
is performed. Raw nonpositive inputs remain visible but are not used. Nonnumeric
or non-finite source values are missing, with a source warning. Identical
duplicates collapse to one observation; conflicting duplicates become missing
and are reported. No valid real months means an unavailable mean/index, not a
division by zero. Nominal display and CSV remain usable independently.

**100 means the selected sample's average**, not absolute PPP or an exchange-rate
fair-value estimate. With RER = 2, 4, 6 the mean is 4 and the real index is
50, 100, 150. Selecting just the first two months changes the mean to 3 and
the index to approximately 66.6667, 133.3333.

### USD and Euro area

When the verified USA FX series is entirely absent, the confirmed USD/USD
accounting identity **1 USD per USD** may be used, explicitly labeled in the
app and export. Access errors never trigger that fallback. Partial missing
USA observations are not filled; no other country's FX observations are filled.

Euro area remains **G163**. CPI history, if supplied by IMF, is not a license to
invent historical euro FX. Euro area FX observations before **1999-01** are
excluded and reported. National currencies are never spliced into this series.

**Observed source limitation on 2026-10-26:** the exact requested
`G163.CPI._T.IX.M` returned HTTP 200 containing an empty dataflow row, not CPI
observations. `G163.XDC_USD.PA_RT.M` returned actual monthly observations starting
1999-01. Consequently, USA/G163 currently supports a nominal chart and CSV,
but cannot form the requested real index. The app explains this and does not
substitute HICP, another provider, or another country code.

## CSV schema

The UTF-8 download contains **only the selected inclusive period**, including
incomplete months. Absent/invalid non-finite source values are blank. Floats use
up to 12 significant digits; calculations retain full floating-point precision.

| Column | Meaning |
| --- | --- |
| month | Calendar month, YYYY-MM |
| country_1, country_2 | Readable names |
| country_1_code, country_2_code | IMF identifiers, including G163 |
| P1_CPI, P2_CPI | Source CPI index levels |
| E1_local_per_USD, E2_local_per_USD | Monthly period-average FX inputs |
| S_country2_per_country1 | Actual nominal E2/E1, independent of CPI availability |
| RER | S × P1/P2 for valid months |
| RER_star_sample_mean | Selected valid-month arithmetic mean, repeated for the sample; blank if none |
| Real_Index | 100 × RER/RER_star; blank for invalid months |
| valid_RER_month | True/False validity flag |
| E1_USD_identity, E2_USD_identity | Explicit accounting-identity provenance |

## Validation

Run from the application folder in the activated environment:

```bash
python -m py_compile app.py
python app.py --self-test
python app.py --live-smoke
```

`--self-test` uses only deterministic synthetic fixtures, never source or displayed
economic data. It covers E2/E1 direction; the 75 RER example; 2/4/6 normalization;
recalculation for a changed sample; reciprocal rates; missing CPI with intact
nominal values; exact selected-month CSV rows and chart bounds; real mean 100;
separate axes; no-valid-month handling; zero/negative/non-finite values; monthly
formats; duplicate conflicts; wrong-series/schema rejection; SCALE/UNIT_MULT;
no pre-euro FX; country roster; empty SDMX dataflow rows; inherited concept
representations; SDMX version ranges; request versions/keys/filters; retries;
optional subscription headers; and the narrowly scoped USD identity fallback.

`--live-smoke` makes genuine read-only IMF metadata/data requests for **USA/JPN**
and **USA/G163**. It prints per-series results and checks the normalized sample.
Exit 0 requires all requested inputs and both real samples; exit 1 means a live
input or calculation is unavailable. An empty G163 CPI is reported as a failed
live-data requirement, never a passing real-index test. This does not prevent
the application from correctly displaying the remaining nominal data.

The validation record below distinguishes offline checks, live source behavior,
and UI functionality. No new-task environment restoration or publication is
claimed by these checks.

### Results recorded on 2026-10-26

- Python 3.12.14; Streamlit 1.66.1, pandas 2.3.3, requests 2.34.2,
  Plotly 6.9.0, pycountry 24.6.1. Dependency installation was repeated
  successfully; `pip check` and Python compilation passed.
- **25 deterministic self-checks passed**. Separate Streamlit interaction checks
  passed for changed sample bounds and renormalization, missing CPI with intact
  nominal values, reversed-date validation, identical-country validation, and
  the unavailable-source state (synthetic fixtures, not live economic data).
- Live IMF metadata, ordered dimensions, codelist meanings, concrete versions,
  period filters, and real SDMX CSV parsing were verified for CPI and ER.
- Live **USA/JPN**: 608 monthly rows in each requested CPI series and 609 in
  each FX series. The default October 2016–September 2026 window had **118/120
  valid real months**, **120/120 nominal months**, and real-index mean **100.0**.
- Live **USA/G163**: 332 genuine G163 FX rows from January 1999; the requested
  G163 CPI had no observations. The default window retained **119/120 nominal
  months**, **0/120 real months**, and the missing September 2026 FX month.
  `--live-smoke` therefore exited **1** for that missing CPI and real sample;
  a fully populated Euro area real index was **not** validated.
- The actual Streamlit app passed live interaction checks for both pairs:
  charts, download controls, full calendar tables, coverage tables, and the
  explicit G163 CPI explanation rendered without exceptions. The server
  started and its health and root HTTP responses both returned **200**.
- The supplied prompt document is preserved byte-for-byte in the log below.
  Exactly the three requested application files are present; environment
  dependencies and temporary validation artifacts are outside the checkout.

### Manual UI checks

- Change either country; confirm labels, direction, and download filename update.
- Change either month; confirm all real values and the CSV use the new window.
- Select the same country twice or reverse the sample bounds; confirm clear errors.
- Toggle Real/Nominal/Both; confirm true nominal units and two axes in Both.
- Check an incomplete sample; confirm blank CSV inputs and gaps in real lines.
- Choose G163 and a window starting before 1999; confirm no pre-euro nominal data.
- Remove network access; confirm a clear source error, without demonstration data.

## Troubleshooting and limitations

- **Proxy/network denial, connection failure or timeout:** allow the IMF hosts
  in environment settings, save applicable runtime changes, then refresh.
- **HTTP 401/403 from IMF:** check the actual deployment's access requirements;
  provide the optional subscription key securely only if required. Never disable
  TLS verification. A missing key variable alone does not prove a key is needed.
- **HTTP 429/5xx:** the app retries with backoff; wait before refreshing again.
- **204, 404, empty CSV or an empty dataflow row:** there are no observations for
  that exact series. Try another period/pair or use independently available nominal
  data. The app does not fabricate missing series.
- **Unexpected metadata/CSV schema:** inspect the reported dimension/key details.
  A provider schema change requires a reviewed parser update, not guessed data.
- **No overlapping valid real months:** inspect coverage for all four inputs;
  use a narrower overlapping period or another pair. Nominal needs only FX.
- **Dependency/import errors:** activate the Python 3.12 virtual environment and
  repeat `python -m pip install -r requirements.txt`. Avoid mixing environments.
- **CPI bases/revisions:** countries need not have the same CPI reference year;
  a fixed base ratio cancels under sample normalization, but changes of base,
  revision methodology, series breaks and missing periods can alter the result.
- **Currency regimes:** redenominations, pegs, official/market-rate distinctions,
  euro membership changes and other regime changes complicate historical
  comparisons. The app does not repair or splice currencies or measure PPP levels.
- This is a bilateral, CPI-based index, not the IMF's multilateral real effective
  exchange rate index. Data freshness and coverage depend on the IMF releases.

## Prompt and model log

**Prompt date:** 2026-10-26

**Prompt-author model:** OpenAI GPT-6 (as identified in the supplied document)

**Actual Codex model:** Codex model not reported

The supplied document is preserved below verbatim. Its model attribution is the
document author's record, not an inference about this execution's model. The
build and subsequent verification pass were performed on 2026-10-26.

````text
# Codex Prompt Record: IMF Real Exchange Rate Explorer

**Prepared on:** October 26, 2026  
**Prompt-author model:** OpenAI GPT-6  
**Target coding environment:** OpenAI Codex (record the actual Codex model used when you run the prompt; do not assume it is GPT-6)  
**Runtime:** Python 3.12  
**Required app deliverables:** `app.py`, `requirements.txt`, `README.md` (exactly these three files in the application folder).

## Prompt 1 — Build the application

You are a senior Python engineer, economic data specialist, and software quality reviewer. Build a working, polished Streamlit app called **IMF Real Exchange Rate Explorer** using Python 3.12. Write complete, runnable code directly into **exactly three application files: `app.py`, `requirements.txt`, and `README.md`**. Do not return pseudocode, incomplete functions, TODO placeholders, or fabricated data. Make reasonable engineering choices without asking me questions.

### 1. App purpose and interface

- Provide two searchable dropdowns: **Country 1** and **Country 2**, drawn from the IMF country universe, plus **Euro area (IMF code `G163`)**. Use readable labels and preserve the corresponding IMF codes. Prefer IMF metadata/codelists as the authoritative source of eligible country codes; use a well-documented, complete, maintainable fallback if metadata is inaccessible. Exclude unrelated regional aggregates. Do not silently replace IMF codes with ISO codes for `G163`.
- Default to a demonstrable pair such as **United States (`USA`) and Japan (`JPN`)**, and reject identical selections with a clear message.
- Provide inclusive **start-month** and **end-month** selectors, with the earliest possible request set to **January 1976** and the latest limited by available data. Show a clear error when start exceeds end. A user can change either month without restarting the app.
- Provide an interactive **Real / Nominal / Both** chart selector, Plotly charts with hover tooltips, informative axes and legends, a data-coverage summary, and a prominent **Download CSV** button.
- Show both the selected country names and the direction of the nominal rate: **units of Country 2 currency per 1 unit of Country 1 currency**. Explain that a Real Index value of 100 is the average over the selected valid months, not a universal purchasing-power-parity level.

### 2. IMF data acquisition and correctness

- Use the IMF SDMX 3.0 API as the primary source: `https://api.imf.org/external/sdmx/3.0`.
- Retrieve monthly observations beginning **1976-01 where available**, without claiming series exist before they actually begin.
- Begin by validating the IMF dataflows, SDMX key dimension order, version identifiers, period-filter syntax, and response format against live API calls and/or IMF metadata. The expected dataflows are **`IMF.STA/CPI`** and **`IMF.STA/ER`**. Treat these as starting points, not permission to invent a successful response.
- Use these monthly series keys:
  - CPI: `{IMF_COUNTRY_CODE}.CPI._T.IX.M`
  - Period-average local-currency units per USD: `{IMF_COUNTRY_CODE}.XDC_USD.PA_RT.M`
  - For the Euro area, substitute `G163` for the country code.
- Construct requests using the actual supported SDMX 3.0 data URL, such as `/data/dataflow/{agency}/{dataflow}/{version}/{key}`, with a verified version marker. Parse genuine IMF SDMX CSV or JSON observations and monthly periods accurately. Handle both `YYYY-MMM`/`YYYY-M01`-style periods and ordinary `YYYY-MM` robustly where encountered; never misinterpret the month.
- If the API requires credentials, support an optional environment variable `IMF_SDMX_SUBSCRIPTION_KEY` in the request headers; never hardcode or publish a key. If access is denied or the IMF is unavailable, report that transparently instead of fabricating data or silently substituting another provider.
- Implement request timeouts, appropriate retries/backoff (particularly HTTP 429 and 5xx), caching with an explicit time-to-live, and clear errors for 204/empty results, unexpected schemas, missing series, API failures, and rate limits.
- Verify the CPI values are index levels (not monthly inflation rates) and the exchange-rate observations are **monthly period averages**. Show source names, requested series keys, and data-access details in the app or an expandable methodology section.
- Handle currencies carefully: for a confirmed USD-to-USD identity, use **1 local USD per USD** when an IMF USA exchange-rate series is missing; identify this as an accounting identity, not an imputed observation. Do not fill any other absent observations. Do not splice pre-euro national currencies onto the Euro area series, extrapolate pre-1999 Euro area exchange rates, or invent historical series.

### 3. Economic definitions and required formulas

For each month `t`, use the definitions below exactly:

- `E1(t)` = Country 1 local-currency units per USD, from the IMF exchange-rate series.
- `E2(t)` = Country 2 local-currency units per USD, from the IMF exchange-rate series.
- `S(t) = E2(t) / E1(t)` = **Country 2 currency units per one Country 1 currency unit**. Do not invert this ratio.
- `P1(t)` and `P2(t)` = Country 1 and Country 2 monthly CPI index levels.
- `RER(t) = S(t) * P1(t) / P2(t)`.
- Within the **user-selected inclusive start/end months**, define valid months as those with all required fields present, numeric, finite, and strictly positive. Compute `RER_star = arithmetic_mean(RER(t))` **only over valid months in that selected sample**, with equal weight per valid month.
- `Real_Index(t) = 100 * RER(t) / RER_star` for each valid selected month. Recalculate `RER_star` and all Real Index values immediately whenever the selection or selected date range changes. The mean Real Index across valid months should be 100, up to floating-point tolerance.
- Plot the **nominal `S(t)` as the unnormalized actual currency exchange rate**, not as an index and not rebased to 100. Never use the real-index denominator for the nominal series.
- Treat missing, zero, negative, duplicate/conflicting, or non-finite inputs explicitly; never conceal observations by forward filling, interpolating, or replacing missing monthly data with zeros. Align all raw series on monthly timestamps before calculations.

### 4. Charting and downloads

- **Real:** show only `Real_Index(t)` with the y-axis clearly labeled `Real exchange rate index (sample average = 100)` and an optional horizontal reference line at 100.
- **Nominal:** show only `S(t)` with the y-axis labeled in **Country 2 currency per Country 1 currency**. Preserve true values and meaningful precision.
- **Both:** show the two series in one interactive chart using **separate, labeled y-axes** (Plotly secondary-axis support). Tooltips must identify which units each series uses. The selected time window must determine the x-axis range.
- Create a calendar-month-aligned data table for **every month within the chosen sample**, including incomplete months, with blanks/NA where source data are absent. Download it as UTF-8 CSV with at least these columns: `month`, `country_1`, `country_2`, `country_1_code`, `country_2_code`, `P1_CPI`, `P2_CPI`, `E1_local_per_USD`, `E2_local_per_USD`, `S_country2_per_country1`, `RER`, `RER_star_sample_mean`, `Real_Index`, and `valid_RER_month`. Record the sample dates, series descriptions, and IMF source in the interface and/or README. Include only the selected period, not the complete retrieved history.
- Indicate the **number of valid RER months versus total selected calendar months**, earliest/latest available dates, and missing-data limitations. If no valid RER months exist, do not divide by zero; present an actionable explanation and still allow any independently available nominal series to display.

### 5. Architecture, README, and reproducibility

- Prefer **Streamlit + pandas + requests + Plotly** and minimal additional Python 3.12-compatible dependencies. Keep network, parsing, country mapping, transformation, calculation, chart creation, and export logic in clean reusable functions in `app.py`. Use Streamlit caching judiciously.
- `requirements.txt` must include every direct dependency with compatible version constraints.
- `README.md` must include: project overview; installation steps; `python -m pip install -r requirements.txt`; `streamlit run app.py`; optional credentials; source API/dataflow/key details; full formulas with conversion direction; treatment of missing data and Euro area historical coverage; explanation of sample-average normalization; CSV fields; troubleshooting for API and dependency errors; validation procedures; and limitations (including possible CPI base revisions and changing currency regimes).
- **Preserve an AI assistance record in `README.md`** headed `Prompt and model log`: (1) prompt date **2026-10-26**; (2) prompt-author model **OpenAI GPT-6**; (3) the *actual Codex model* if the environment reveals it, otherwise write `Codex model not reported`; and (4) the **complete prompt text you received, verbatim**. If subsequent prompts are used, append their dates, model identifiers when known, and verbatim texts in this same README section. This logging is part of the required deliverable; do not create a fourth application file solely for the log.

### 6. Tests and acceptance checks

Write deterministic, callable self-checks within `app.py` (because the delivery must contain only three files), and explain how to run them in `README.md`. Check at least:

1. If `E1 = 2` and `E2 = 100`, `S` must equal **50**, never 0.02.
2. If additionally `P1 = 120` and `P2 = 80`, `RER` must equal **75**.
3. Given valid monthly RER values of **2, 4, and 6**, `RER_star` must be **4**, and the corresponding Real Index values must be **50, 100, and 150**.
4. Changing the sample to include only the first two months must change `RER_star` to **3** and the Real Index to approximately **66.6667 and 133.3333**.
5. With both raw exchange rates present and positive, swapping Country 1 and Country 2 must make the nominal cross-rate reciprocal.
6. Missing CPI for a month excludes it from the RER sample mean but must not cause an otherwise valid nominal value to disappear.
7. CSV rows and chart data correspond precisely to the currently selected period, and the real index sample mean is approximately 100 over valid months.

If a live network environment is available, perform at least one smoke test with a normal IMF pair and one using `G163`; inspect actual IMF responses, then fix any API parsing or key issues. If network access is unavailable, do not pretend those tests passed. Report exactly what was and was not validated.

### Completion criteria

Produce all three complete files on disk. Run syntax checking and deterministic tests when execution tools are available. The app must remain useful even when an IMF series has gaps, and it must never silently invent economic observations. Finish with concise instructions for launching the app and a summary of verified versus unverified live API behavior. Do not substitute an explanation for any of the three complete application files.

## Prompt 2 — Optional Codex verification pass

Review the three existing files (`app.py`, `requirements.txt`, `README.md`) against the original request and execute the deterministic checks. Focus on IMF API access/response parsing, the `E2/E1` exchange-rate direction, equal-weight monthly `RER_star` computed only over selected valid months, full re-normalization after date changes, truly unnormalized nominal exchange rates, separate y-axes in Both mode, and all selected-month CSV rows including missing observations. Repair any failures directly in the three existing files. Test the Euro area (`G163`) without inventing data before euro adoption. Update the README `Prompt and model log` to include this **second prompt verbatim**, today's execution date, and the actual Codex model if available (otherwise mark it not reported). Report test outcomes factually. Do not create additional application files.

## Reference starting points

- IMF official API page: https://data.imf.org/en/Resource-Pages/IMF-API
- IMF SDMX 3.0 API base: https://api.imf.org/external/sdmx/3.0
- The `IMF.STA/CPI` and `IMF.STA/ER` dataflows and SDMX keys must be checked against the live API or its metadata, not assumed to be continuously available.

````

### Build authorization

Execution date: 2026-10-26. Actual Codex model: Codex model not reported.

```text
use them to build the app in real-exchange-rate-app
```

### Verification pass

Execution date: 2026-10-26. Actual Codex model: Codex model not reported.
The second prompt was applied as part of completing the build. Verbatim text:

```text
Review the three existing files (`app.py`, `requirements.txt`, `README.md`) against the original request and execute the deterministic checks. Focus on IMF API access/response parsing, the `E2/E1` exchange-rate direction, equal-weight monthly `RER_star` computed only over selected valid months, full re-normalization after date changes, truly unnormalized nominal exchange rates, separate y-axes in Both mode, and all selected-month CSV rows including missing observations. Repair any failures directly in the three existing files. Test the Euro area (`G163`) without inventing data before euro adoption. Update the README `Prompt and model log` to include this **second prompt verbatim**, today's execution date, and the actual Codex model if available (otherwise mark it not reported). Report test outcomes factually. Do not create additional application files.
```
