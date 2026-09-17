# Licensing decisions required

Status: open decisions, none made. This records what must be chosen, what each
choice costs, and which choices stop being available over time. It is not legal
advice; the recommendations below are engineering judgment and the irreversible
ones should be confirmed with a lawyer before announcement. Recorded 2026-09-17.

## Current state

`LICENSE` is MIT, copyright 2026 ForgeStack. There is no `CONTRIBUTING`, no
Developer Certificate of Origin, no contributor licence agreement, and no
`NOTICE`. Copyright is currently held by one party, which is what makes several
of the decisions below still open.

The product split these decisions serve is in `docs/PRODUCT_BOUNDARY.md`.

## Urgency

| # | Decision | Deadline | Reversible after? |
| --- | --- | --- | --- |
| 1 | Contributor agreement: DCO, CLA, or neither | First outside contribution | No |
| 2 | Open licence: permissive or protective | Public announcement | Only with decision 1 in place |
| 3 | Third-party code intake policy | First vendored dependency | Per dependency, expensively |
| 4 | Licence for `anvil-cloud` and the paid UI | Before that repository is shared | Yes |
| 5 | Trademark and name | Before announcement | Partially |

Decisions 1 and 2 are the urgent pair, and they are coupled: decision 1 is what
keeps decision 2 open.

## Decision 1: contributor agreement

**Choose before merging any contribution from outside ForgeStack.**

Without an agreement, every outside contributor retains copyright in their
contribution. Relicensing the project then requires locating and getting
permission from each of them, which in practice means the licence is frozen
permanently the day the first outside pull request merges.

- **DCO** (a `Signed-off-by` line, enforced by CI): low friction, widely
  understood, asserts the contributor had the right to submit. It does **not**
  transfer copyright or grant relicensing rights.
- **CLA**: contributors grant a licence broad enough to relicense. Preserves
  future flexibility; adds signing friction and deters casual contributors.
- **Neither**: fastest today, and forecloses decision 2 permanently.

Recommendation: adopt a CLA if there is any chance of decision 2 landing on a
protective licence later, since that is precisely the option it preserves. Adopt
a DCO if decision 2 is settled now as permanently permissive. Do not ship a
public repository with neither. The cost today is an afternoon; the cost after
the fact is unrecoverable.

## Decision 2: permissive or protective open licence

**Choose before public announcement.**

- **MIT (current)**: maximum adoption; anyone, including a well-funded
  competitor, may host a competing Anvil service. Gives no protection to the
  paid tier.
- **Apache-2.0**: permissive like MIT, plus an express patent grant and patent
  retaliation clause. Preferred by enterprise legal review. Compatible with
  vendoring the Apache-2.0 code covered in decision 3.
- **AGPL-3.0**: a competitor hosting a modified Anvil must publish their
  modifications. Deters hosted competition, but many enterprises prohibit AGPL
  outright, so it taxes the adoption the open half exists to earn.
- **BSL or similar source-available**: directly forbids competing hosted
  offerings, usually converting to an open licence after a delay. Effective
  protection, and it costs the "open source" claim, which parts of the intended
  audience treat as disqualifying.

Recommendation: stay permissive, and move from MIT to Apache-2.0. At this stage
adoption is worth more than protection, the hosted-competitor risk is
speculative, and the moat identified in `docs/PRODUCT_BOUNDARY.md` is pooled data
rather than source code, which no licence protects anyway. Apache-2.0 keeps the
permissive posture while adding the patent grant enterprises ask for and removing
the friction in decision 3.

This recommendation is only safe to act on if decision 1 has been made, since
that is what keeps a protective licence available if the assessment changes.

## Decision 3: third-party code intake

**Choose before vendoring any outside source.**

Anvil today has no third-party dependencies, which is an asset that should be
spent deliberately. The concrete case already considered was copying WebView
shells from an Apache-2.0 project to obtain macOS, iOS, and Android clients.

Copying Apache-2.0 source into an MIT repository is permitted, but it is not
free: the copied subtree stays Apache-2.0, its copyright notice and licence text
must ship with it, `NOTICE` attribution must propagate, and modifications must be
marked. The result is a mixed-licence repository that every downstream consumer
must reason about.

Recommendation: adopt a written policy that vendored code lands in a clearly
marked subdirectory with its own `LICENSE` and an entry in a root `NOTICE`, and
is never mixed into `src/anvil/`. If decision 2 selects Apache-2.0, this friction
largely disappears for Apache-2.0 sources, which is a further argument for it.
Copyleft sources remain incompatible with the paid split and should be refused
rather than negotiated.

## Decision 4: licence for `anvil-cloud` and the paid UI

**Choose before that repository is shared with anyone outside ForgeStack.**

Options are proprietary and closed, source-available for customers, or open with
paid hosting. The paid tier is the revenue, so proprietary is the default and the
burden of argument is on any alternative. This decision is reversible, which is
why it is not urgent.

Note that if decision 2 lands on AGPL, a cloud service built on Anvil requires
care about whether the service itself triggers the network clause. Permissive
licensing removes that question entirely.

## Decision 5: trademark and name

**Choose before announcement.**

A permissive code licence grants no right to the name. Registering "Anvil" as a
trademark and publishing a usage policy is what prevents a fork from marketing
itself as Anvil, and it is the protection that actually applies to a hosted
competitor. It is also cheap relative to relicensing.

Check availability early. The name is common, and there is at least one
established developer-tooling product using it already (Anvil, the Python
full-stack web framework at anvil.works), so a registration in the relevant class
may be contested or unavailable. A collision discovered after announcement is far
more expensive than one discovered now.

## Minimum set before a public repository

1. Decision 1 recorded, with `CONTRIBUTING.md` and CI enforcement if a DCO.
2. Decision 2 recorded, with `LICENSE` and file headers consistent.
3. Decision 3 recorded, even if the answer is that nothing is vendored yet.
4. `NOTICE` present if anything is vendored.
5. Trademark search completed for decision 5.
