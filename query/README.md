# vault-query

Created and maintained by [Marc Jeulin](https://github.com/jeulinmarc), part of [MyOpenFund](https://github.com/MyOpenFund).

Read-only queries over the MyOpenFund vault (a PostgreSQL index of corpus manifests): document
search, corpus stats, vocabulary and run health. Every function takes an open psycopg2
connection owned by the caller and never writes. Import name: `vault_query`.

## Install

```bash
pip install myopenfund-vault-query
```

If this environment installed the old distribution (`vault-query`, from a git URL), run
`pip uninstall -y vault-query` before installing `myopenfund-vault-query`: both own the same import package.

Full reference: the "Read queries" section of the
[vault README](https://github.com/MyOpenFund/vault#read-queries-vault-query).
