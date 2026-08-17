# Choose the async, stream, and cancellation contract

Type: grilling
Status: open
Blocked by: 02, 03

## Question

How does v0 map the Reference Revision's promises, `EventStream`, abort signals, streaming settlement, listener ordering, tool concurrency, and idle lifecycle onto Python `asyncio` without introducing ambiguous ownership or cancellation behaviour?
