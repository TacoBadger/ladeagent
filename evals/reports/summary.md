# Sammenligning af kørsler

Alle kørsler går mod samme frosne snapshot og samme 30 spørgsmål, så forskellen er prompt og model, ikke data.

| Kørsel | Model | Prompt | Backend | numeric | no_data | injection | tone | Total | Pris/samtale | Latens |
|---|---|---|---|---|---|---|---|---|---|---|
| v1_claude-opus-5_cli | claude-opus-5 | v1 | claude-cli | 14/15 | 5/5 | 4/5 | 0/5 | **23/30** (77%) | $0.1024 | 20.3 s |
| v1_claude-sonnet-5_run1 | claude-sonnet-5 | v1 | claude-cli | 15/15 | 5/5 | 3/5 | 4/5 | **27/30** (90%) | $0.0366 | 21.07 s |
| v2_claude-fable-5-1_run1 | claude-fable-5-1 | v2 | claude-cli | 14/15 | 5/5 | 5/5 | 4/5 | **28/30** (93%) | $0.1190 | 13.73 s |
| v2_claude-fable-5-1_run2 | claude-fable-5-1 | v2 | claude-cli | 14/15 | 5/5 | 5/5 | 4/5 | **28/30** (93%) | $0.1166 | 13.5 s |
| v2_claude-haiku-4-5_cli | claude-haiku-4-5 | v2 | claude-cli | 8/15 | 5/5 | 4/5 | 2/5 | **19/30** (63%) | $0.0236 | 20.08 s |
| v2_claude-haiku-5-5_run1 | claude-haiku-5-5 | v2 | claude-cli | 14/15 | 5/5 | 5/5 | 2/5 | **26/30** (87%) | $0.0020 | 9.05 s |
| v2_claude-haiku-5-5_run2 | claude-haiku-5-5 | v2 | claude-cli | 15/15 | 5/5 | 5/5 | 5/5 | **30/30** (100%) | $0.0020 | 9.96 s |
| v2_claude-opus-5_cli | claude-opus-5 | v2 | claude-cli | 15/15 | 5/5 | 5/5 | 3/5 | **28/30** (93%) | $0.0853 | 15.1 s |
| v2_claude-sonnet-5-5_run1 | claude-sonnet-5-5 | v2 | claude-cli | 15/15 | 5/5 | 5/5 | 2/5 | **27/30** (90%) | $0.0328 | 12.95 s |
| v2_claude-sonnet-5-5_run2 | claude-sonnet-5-5 | v2 | claude-cli | 15/15 | 5/5 | 5/5 | 3/5 | **28/30** (93%) | $0.0311 | 11.9 s |
| v2_claude-sonnet-5_cli | claude-sonnet-5 | v2 | claude-cli | 15/15 | 5/5 | 5/5 | 4/5 | **29/30** (97%) | $0.0408 | 16.17 s |
| v2_claude-sonnet-5_run1 | claude-sonnet-5 | v2 | claude-cli | 14/15 | 5/5 | 5/5 | 5/5 | **29/30** (97%) | $0.0343 | 19.74 s |
| v2_claude-sonnet-5_run2 | claude-sonnet-5 | v2 | claude-cli | 14/15 | 5/5 | 5/5 | 4/5 | **28/30** (93%) | $0.0366 | 19.41 s |
