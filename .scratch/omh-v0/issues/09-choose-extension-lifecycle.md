# Choose the Python Extension lifecycle

Type: grilling
Status: open
Blocked by: 01, 03, 04, 05, 07, 08

## Question

What discovery order, module contract, registration interface, event set, dependency access, cache policy, reload lifecycle, stale-context rule, trust gate, and error isolation must dynamically loaded `.py` extensions provide in v0? This ticket owns the Python entrypoint corresponding to the Reference Extension default-export factory and, if that mapping differs inside an envelope, must write a complete separately named PA; it may not reuse `PA:python-illegal-identifier`.
