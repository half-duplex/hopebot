# HOPEBot
The HOPE CoreBot usually running at @corebot:hope.net

## Bot Setup
Install and configure PostgreSQL. Create a `hopebot` Linux user.
In `psql`:
```sql
CREATE ROLE hopebot LOGIN;
CREATE DATABASE hopebot OWNER hopebot;
```
- Follow the [maubot install instructions](https://docs.mau.fi/maubot/usage/setup/index.html)
- Follow the [maubot encryption instructions](https://docs.mau.fi/maubot/usage/encryption.html)
- Install perlin-numpy with `pip install -U git+https://github.com/pvigier/perlin-numpy`
- Install the pypi dependencies with `pip install -U PySide6 numpy`
- Configure mbc by running `mbc login`
- Authenticate maubot to Matrix with `mbc auth --update-client`
- Clone this repository, and with the virtualenv active, from inside this folder run `mbc build --upload`
- From the web panel, configure the maubot client and create an Instance for this plugin, then configure it.
- To update the "prod" instance, remove ".beta" from the `id` field of
  `maubot.yaml` before uploading

## Account Setup
The bot will not be able to operate without modifying the default rate limits.
Using a Synapse admin account, you can call e.g.
```
curl 'https://hope.ems.host/_synapse/admin/v1/users/%40corebot%3Ahope.net/override_ratelimit' \
    -H 'authorization: Bearer foobar' -X POST --data '{"messages_per_second":0,"burst_count":0}'
```

## Usage
Users can:
- Redeem tokens by just sending them to the bot
- Find the room for a talk by posting a pretalx link
- Say `!help` to get the configured help text
- Say `!mod [message]` to request a moderator's presence

For bot owners, the following commands are available:
- `!load_tokens /path/to/attendee.txt attendee [clear|truncate]` - Load a set of tokens,
  optionally removing same-type (clear) or ALL (truncate) tokens first.
- `!mark_unused token|hash` - Allow the token to be used again
- `!op [!room:id.x] [@user:id.x]` - Give someone admin perms in a room
- `!order space room|"here" [order]` - Change space child order. Omit order
  to clear. [Docs](https://spec.matrix.org/latest/client-server-api/#ordering-of-children-within-a-space)
- `!speaker @foo:example.com [talk shortcode|link]` - Mark a user as a speaker
  and give moderator permissions in that talk's room
- `!stats` - Show count of loaded and redeemed tokens
- `!sync_talks` - Synchronize scheduled talks to rooms
- `!token_info token|hash` - Show who redeemed a token and when

## Matrix Setup
To prepare a conference:
- Create private spaces for attendees, volunteers, and presenters
- Within the attendee space create Talks and Community Rooms spaces
- In the attendee space, create rooms:
  Announcements, Press, General, Info Desk, Tech Support, Feedback, Local
  Attractions, Projects, Jobs & Gigs, CTF, etc
- In the presenter space, create: Presenter Announcements, Presenter Helpdesk
- In the volunteer space, create:
  Volunteer Announcements, Volunteer General, Volunteer Off-Topic, Matrix
  Moderation, Loadout, Info Desk Team, Digital Infrastructure, A/V, Network,
  Web, Volunteer Feedback
- In each room, set history and permissions:
  - Who can read history? = Members only (since selecting this option)
  - If Announcements, Send messages = Moderator
  - If Press, Access = Private (invite only)
  - Invite users = Admin
- Put the space and room IDs into appropriate places in the bot config
- Update the rest of the bot config (token format, admins, etc)
- Generate tokens with `uv run hopebot-tokens`, load with `!load_tokens`
- If one token type (e.g. presenter) should also grant access to another
  space (e.g. attendee), load the list a second time with the other type.

## Contributions
Contributions are welcome. They must be licensed under EUPL-1.2, linted with `flake8`, formatted with
`black`, type-hinted where possible, and tested.
