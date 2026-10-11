# Request-abuse exercise on the GX10

This standalone driver sends actual GET requests from another LAN machine to Driftwood. It does not inject a
deployment, modify configuration, or manually create an OCA incident. It is a
separate client, not a real compromise or evidence of attacker attribution.

The inspected Driftwood baseline already caches `/bestsellers` and coalesces
concurrent cache misses. Arbitrary query parameters do not bypass its fixed
cache key. The default target is therefore the uncached `/products/1` database
lookup. That query is indexed: this is request-volume abuse, not yet a proven
expensive-query exploit. Do not claim a demonstrated outage without measuring it.

## Run

Start healthy Driftwood and OCA on the GX10 using the existing real-mode setup:
`OCA_RUNNER=real`, `OCA_FAKECO_ROOT` pointing to the Driftwood clone, and
`OCA_AUTOSTART_PIPELINE=true`. Confirm OCA has passing health checks and no
blocking ticket for orders-api. Keep normal storefront/loadgen activity running
so customer behavior contributes to the health metrics.

Copy only `scripts/request_abuse.py` to the attacker laptop. It needs Python 3,
no packages, no OCA imports, no OCA credentials, and no FakeCo checkout. On the
GX10, `hostname -I` shows candidate LAN addresses. Confirm the frontend's configured
port (default 8080) and reachability from the laptop before starting traffic.
Replace `192.168.1.50` below with the GX10's LAN IP.

From the attacker laptop:

```bash
python3 request_abuse.py --target http://192.168.1.50:8080/api --seconds 900 --concurrency 10 --rps 20 > request-abuse.jsonl
```

The first 30 seconds record customer probes without attack traffic. Then bounded
traffic starts, while `/products/2` is probed every two seconds. Both endpoints
are read-only. Ctrl+C stops submission; in-flight requests have bounded timeouts.
No redirects are followed; loopback and literal private LAN IPs are accepted.

For Windows, use `python` instead of `python3`. To watch output live in PowerShell,
replace the redirection with `| Tee-Object -FilePath request-abuse.jsonl`.

Run customer observations from a **different source IP** (another machine, or
the GX10 via its LAN address), especially when testing per-client rate limiting:

```bash
python3 request_abuse.py --target http://192.168.1.50:8080/api --probe-only --baseline-seconds 30 --seconds 900 > customer-probes.jsonl
```

The probe bundled with the attack process shares its source IP and can be
blocked by a correct per-IP mitigation. Its recovery alone is therefore not the
customer success criterion. Start the independent observer first, then the
attacker. Watch OCA's existing dashboard on the GX10; the attacker never calls it.

For a stronger measured exercise, increase gradually, up to 32 workers and
100 requests/second. Limits cap client submission; they do not guarantee backend
capacity or a particular number of failures. If the frontend is on a different
port, use `--target http://127.0.0.1:PORT/api`. Direct API testing uses
`--target http://127.0.0.1:8000`, but bypasses frontend mitigations.

## Evidence and completion criteria

Record baseline customer latency, attack start, health breaches, OCA ticket and
diagnosis, sandbox BEFORE/AFTER, Guardian/gate outcome, deployment, and customer
probe recovery **before traffic finishes**. A 429 is useful mitigation evidence
for abusive requests; it is not a successful customer response. A stopped attack
followed by recovery does not demonstrate a repair.

## Next real-infrastructure run

Wait for the teammate remediation update; the local sandbox replay prototype has been removed. Update and restart OCA and FakeCo on the GX10, confirm a healthy customer baseline, then repeat the external session-request workload. Verify detection, sandbox reproduction, review, deployment, and customer recovery while abuse continues. The standalone driver does not create tickets or alter FakeCo.

The previous loyalty exercise triggered pool saturation and an OCA ticket, but sandbox reproduction failed and no repair deployed. Recovery followed stopping the traffic. Keep that distinction in the demo results.
