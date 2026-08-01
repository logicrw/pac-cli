# Quality fixture provenance

Updated for the PAC 0.2.1 public release on 2026-08-01.

## `full/`

These 12 fixtures are **text-only excerpts, not whole web pages**. Each begins with its title, agency attribution, and exact source URL, followed by unpadded agency-authored prose. Images, captions, logos, linked documents and journal abstracts are excluded. Direct quotations from non-federal people are excluded; some federal-official quotations are also omitted. The excerpts are long enough to exercise `quality_check` without synthetic repetition or padding. Expected result: 12/12 pass (zero false positives).

### Manifest

| Fixture | Title | Agency | Exact source | Public-domain basis |
|---|---|---|---|---|
| `sample1.txt` | Touchdown! Carrying NASA Science, Firefly’s Blue Ghost Lands on Moon | NASA | https://www.nasa.gov/news-release/touchdown-carrying-nasa-science-fireflys-blue-ghost-lands-on-moon/ | §105; NASA policy |
| `sample2.txt` | NASA Says Mars Rover Discovered Potential Biosignature Last Year | NASA | https://www.nasa.gov/news-release/nasa-says-mars-rover-discovered-potential-biosignature-last-year/ | §105; NASA policy |
| `sample3.txt` | NOAA ocean outlook projects cooler deep waters for Gulf of Maine | NOAA | https://www.noaa.gov/news-release/noaa-ocean-outlook-projects-cooler-deep-waters-for-gulf-of-maine | §105; NOAA policy |
| `sample4.txt` | NIST Finalizes ‘Lightweight Cryptography’ Standard to Protect Small Devices | NIST | https://www.nist.gov/news-events/news/2025/08/nist-finalizes-lightweight-cryptography-standard-protect-small-devices | §105; NIST policy |
| `sample5.txt` | Twins grow more slowly in early pregnancy than previously thought | NIH | https://www.nih.gov/news-events/news-releases/twins-grow-more-slowly-early-pregnancy-previously-thought | §105; NIH policy |
| `sample6.txt` | SEC Announces Formation of Cross-Border Task Force to Combat Fraud | SEC | https://www.sec.gov/newsroom/press-releases/2025-113-sec-announces-formation-cross-border-task-force-combat-fraud | §105; SEC policy |
| `sample7.txt` | USGS releases a comprehensive look at water resources in the United States | USGS | https://www.usgs.gov/news/national-news-release/usgs-releases-a-comprehensive-look-water-resources-united-states | §105; USGS policy |
| `sample8.txt` | Energy Department Announces Actions to Secure American Critical Minerals and Materials Supply Chain | DOE | https://www.energy.gov/articles/energy-department-announces-actions-secure-american-critical-minerals-and-materials-supply | §105; DOE policy |
| `sample9.txt` | EPA Protects the Little Colorado River from Impacts of Abandoned Uranium Mines, Announces Removal Action to Advance Cleanup of Contamination | EPA | https://www.epa.gov/newsreleases/epa-protects-little-colorado-river-impacts-abandoned-uranium-mines-announces-removal | §105; agency-authored federal press release |
| `sample10.txt` | CDC Reports Nearly 24% Decline in U.S. Drug Overdose Deaths | CDC | https://www.cdc.gov/media/releases/2025/2025-cdc-reports-decline-in-us-drug-overdose-deaths.html | §105; CDC reuse policy and required attribution/non-endorsement notice in fixture |
| `sample11.txt` | Ever-changing universe revealed in first imagery from NSF-DOE Vera C. Rubin Observatory | NSF | https://www.nsf.gov/news/first-imagery-nsf-doe-vera-c-rubin-observatory | §105; NSF policy |
| `sample12.txt` | FTC Sends $126 Million in Refunds to Fortnite Players Who Were Charged for Unwanted Items, Reopens Claims Process | FTC | https://www.ftc.gov/news-events/news/press-releases/2025/06/ftc-sends-126-million-refunds-fortnite-players-who-were-charged-unwanted-items-reopens-claims | §105; FTC policy |

### Copyright and reuse basis

Under [17 U.S.C. §105](https://www.govinfo.gov/content/pkg/USCODE-2023-title17/html/USCODE-2023-title17-chap1-sec105.htm), copyright protection is not available for works of the United States Government. These fixtures reproduce only prose presented as authored by the named federal agencies. Agency guidance: [NASA](https://sti.nasa.gov/disclaimers/), [NOAA](https://library.noaa.gov/blogs/news-research-highlights/question-of-the-quarter-noaacopyright), [NIST](https://www.nist.gov/copyrights-disclaimers), [NIH](https://www.nih.gov/about-nih/frequently-asked-questions), [SEC](https://www.sec.gov/about/webmaster-frequently-asked-questions), [USGS](https://www.usgs.gov/information-policies-and-instructions/copyrights-and-credits), [DOE](https://www.energy.gov/web-policies), [CDC](https://www.cdc.gov/other/agencymaterials.html), [NSF](https://www.nsf.gov/policies/digital), and [FTC](https://www.ftc.gov/policy-notices/website-policy).

Agency pages may contain separately credited or third-party material that is not a U.S. government work; none is intentionally included here. Agency names, seals, logos, and marks can have separate restrictions and are not included. §105 governs U.S. copyright; foreign jurisdictions may recognize rights in U.S. government works, so downstream users must evaluate foreign rights separately. Attribution does not imply agency endorsement.

## `teaser/`

- 10 public, unauthenticated paywall-page text extracts; no credentials and no paywall bypass.
- `sample1.txt` and `sample2.txt` are Barron's teaser bodies emitted by the frozen baseline CLI during the live evaluation.
- `sample3.txt` through `sample10.txt` are public visible-page extracts returned by Exa from Economist article pages.
- Every file embeds its source URL on the first line. The observed extracts are 357–742 characters long and contain no synthetic padding.
- Expected result: at least 9/10 rejected as `PAYWALL_REMAINING` (current result: 10/10).

Verification:

```bash
env -u PYTHONPATH .venv/bin/pytest tests/test_quality.py -v
env -u PYTHONPATH .venv/bin/pytest
git diff --check
```
