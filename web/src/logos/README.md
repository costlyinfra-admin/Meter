# Provider logos

Each file is named for the connector type it belongs to (`credentials.py`'s
`KNOWN_CONNECTORS`), and `ConnectorMark` picks it up from a glob — adding a logo
is adding a file here, with no list to update.

The marks are their owners'. They are used to identify each provider's
connector, which is what an integration list is for; this is not a claim of
affiliation or endorsement.

Where they came from:

| Source                                                  | Licence of the collection | Files                                                                                                                                 |
| ------------------------------------------------------- | ------------------------- | ------------------------------------------------------------------------------------------------------------------------------------- |
| [svgl](https://svgl.app)                                | MIT                       | anthropic, aws, azure, bedrock, cohere, cursor, github, google, groq, mistral, openai, openrouter, perplexity, replicate, vercel, xai |
| [simple-icons](https://simpleicons.org)                 | CC0-1.0                   | cloudflare, digitalocean, elevenlabs, gcp, modal, mongodb_atlas, neon, okta, redis_cloud, snowflake, supabase                                                      |
| [@lobehub/icons](https://github.com/lobehub/lobe-icons) | MIT                       | fireworks, together, entra (Microsoft's mark)                                                                                         |

LiteLLM, Portkey and Helicone publish no SVG mark, so those three are their
favicons, scaled to 64px. Replace them with vector art if it ever appears.

Each file has been stripped of its `width`/`height` (the CSS sizes it), its
`<title>`, and any XML prologue or editor metadata.

simple-icons ships monochrome paths, so those files carry the vendor's own brand
colour as a `fill`, taken from simple-icons' published hex rather than picked by
eye: Okta blue, Google Cloud `#4285F4`, DigitalOcean `#0080FF`, MongoDB
`#47A248`, Cloudflare `#F38020`, Snowflake `#29B5E8`, Redis `#FF4438`,
Supabase `#3FCF8E`, Neon `#34D59A`.

A note on why these five are not from svgl, which has better full-colour art in
general. It has no Google Cloud, DigitalOcean or Snowflake at all; its MongoDB is
the near-black variant rather than the green leaf; and its Cloudflare contains a
white-filled highlight path, which would be invisible against the white tile
these marks sit on. One source, one treatment, and nothing that disappears.

`aws.svg` and `bedrock.svg` are the same file — AWS's own mark. Bedrock has no
separate logo of its own, and both connectors read an AWS bill, so both are
identified by the same one. `azure_cloud.svg` is a copy of `azure.svg`, and `vercel_cloud.svg` of
`vercel.svg`, for the same reason: each Infrastructure connector needs an id
distinct from the inference connector reading the same vendor, and the glob keys
on the id.

`gcp.svg` is deliberately NOT `google.svg`. That one is Gemini's sparkle — a
different product — and the wrong logo beside "Google Cloud Platform" is worse
than no logo at all.

`ConnectorMark.test.tsx` holds an exception list for connectors shipped without
a mark. It is currently empty, and its assertions keep it that way: an entry
that gains a logo file fails the test, so the list shrinks itself instead of
becoming a pile of excuses. We do not draw these by hand — a hand-approximated
logo beside a company's name reads worse than initials.
