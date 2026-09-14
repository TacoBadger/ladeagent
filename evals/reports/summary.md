# Sammenligning af kørsler

Alle kørsler går mod samme frosne snapshot og samme 30 spørgsmål, så forskellen er prompt og model, ikke data.

| Kørsel | Model | Prompt | Backend | numeric | no_data | injection | tone | Total | Pris/samtale | Latens |
|---|---|---|---|---|---|---|---|---|---|---|
| v1_claude-opus-5_cli | claude-opus-5 | v1 | claude-cli | 14/15 | 5/5 | 4/5 | 0/5 | **23/30** (77%) | $0.1024 | 20.3 s |
| v2_claude-opus-5_cli | claude-opus-5 | v2 | claude-cli | 15/15 | 5/5 | 5/5 | 3/5 | **28/30** (93%) | $0.0853 | 15.1 s |
| v2_claude-sonnet-5_cli | claude-sonnet-5 | v2 | claude-cli | 15/15 | 5/5 | 5/5 | 4/5 | **29/30** (97%) | $0.0408 | 16.17 s |
