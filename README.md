# HOPEBot
The HOPE CoreBot usually running at @corebot:hope.net

## Setup
Install and configure PostgreSQL. Create a `hopebot` Linux user.
In `psql`:
```sql
CREATE ROLE hopebot LOGIN;
CREATE DATABASE hopebot OWNER hopebot;
```
Create a virtualenv for maubot and install it from `pip`, then configure and and start it.
Configure `mbc` by putting in it `{"default_server":"http://localhost:29316"}` and running `mbc login`
Authenticate maubot to Matrix with `mbc auth --update-client`
Clone this repository, and with the virtualenv active, from inside this folder run `mbc build --upload`
From the web panel, configure the maubot client and create an Instance for this plugin, then configure it.

## Contributions
Contributions are welcome. They must be licensed under EUPL-1.2, linted with `flake8`, formatted with
`black`, type-hinted where possible, and tested.
