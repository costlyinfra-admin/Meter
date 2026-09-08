# Provider logos

Each file is named for the connector type it belongs to (`credentials.py`'s
`KNOWN_CONNECTORS`), and `ConnectorMark` picks it up from a glob — adding a logo
is adding a file here, with no list to update.

The marks are their owners'. They are used to identify each provider's
connector, which is what an integration list is for; this is not a claim of
affiliation or endorsement.

Where they came from:

| Source                                                  | Licence of the collection | Files                                                                                                                            |
| ------------------------------------------------------- | ------------------------- | -------------------------------------------------------------------------------------------------------------------------------- |
| [svgl](https://svgl.app)                                | MIT                       | anthropic, aws, azure, bedrock, cohere, cursor, github, google, groq, mistral, openai, openrouter, perplexity, replicate, vercel, xai |
| [simple-icons](https://simpleicons.org)                 | CC0-1.0                   | elevenlabs, modal, okta                                                                                                          |
| [@lobehub/icons](https://github.com/lobehub/lobe-icons) | MIT                       | fireworks, together, entra (Microsoft's mark)                                                                                    |

LiteLLM, Portkey and Helicone publish no SVG mark, so those three are their
favicons, scaled to 64px. Replace them with vector art if it ever appears.

Each file has been stripped of its `width`/`height` (the CSS sizes it), its
`<title>`, and any XML prologue or editor metadata. Okta's is monochrome from
simple-icons and carries its brand blue as an added `fill`.

`aws.svg` and `bedrock.svg` are the same file — AWS's own mark. Bedrock has no
separate logo of its own, and both connectors read an AWS bill, so both are
identified by the same one. `azure_cloud.svg` is a copy of `azure.svg` for the same reason: the
Infrastructure tab's Azure provider needs an id of its own, distinct from the
Azure OpenAI connector's, and the glob keys on the id. Google Cloud has no file
yet, so the GCP row shows a monogram; it is listed as coming soon and is not
connectable.
