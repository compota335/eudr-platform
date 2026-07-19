"""Static, hand-maintained encoding of EUDR Annex I (Combined Nomenclature).

Source of truth: **Annex I of Regulation (EU) 2023/1115** (the EUDR), which
lists, for each of the seven in-scope commodities, the Combined Nomenclature
(CN) codes of the "relevant products". Scope under the regulation is decided by
the CN code declared to customs, which is what makes scope-checking a mechanical
table lookup rather than a judgement call (see the deep dive, Stage 1).

This table is maintained BY HAND from the regulation annex and MUST be
reconciled against the consolidated text of Regulation (EU) 2023/1115 (and any
amending act) before it is relied on in production. CN codes are also revised
annually by the Commission's Combined Nomenclature updates; a code family that
splits or renumbers must be re-checked here.

Fidelity policy (house rule: FAIL LOUD, NEVER FAKE SUCCESS):

* We encode the code FAMILIES at heading (4-digit) or subheading (6-digit)
  granularity that are well-established members of Annex I. We deliberately do
  NOT enumerate every 8-digit CN subdivision: the annex mixes whole headings
  ("ex 4401", entire headings, and specific subheadings), and inventing precise
  8-digit codes we are not sure of would be fabricating regulatory data.
* Lookup is prefix-based (see :func:`commodity_for_cn`): a declared code such as
  "090121" (roasted coffee) matches the heading "0901". This mirrors how the
  annex uses headings to sweep in all of their subdivisions. Where the annex
  takes only PART of a heading ("ex ...") this table is therefore broader than
  the annex; such a match is a *candidate* in scope that a human must confirm
  against the exact annex line. This is the conservative direction: we flag for
  confirmation rather than silently wave a product through.
* When unsure of a specific code, we include only the heading-level codes we are
  confident sit in Annex I, or we omit it. Better fewer correct codes than
  fabricated precise ones.

The entries below are grouped by commodity with the annex rationale in a comment
so the table can be audited line by line against the regulation.
"""

from __future__ import annotations

from app.models.enums import Commodity

# Bump this whenever the table content changes so any stored scope decision can
# be traced back to the exact Annex I encoding that produced it.
CN_CODE_TABLE_VERSION = "2023.1115.annex1"


# --------------------------------------------------------------------------- #
# Annex I encoding: CN heading/subheading -> Commodity                         #
# --------------------------------------------------------------------------- #
# Keys are normalized CN codes (digits only, no dots/spaces). Values are the
# Annex I commodity the code family belongs to. Comments cite the product family
# each heading covers so the mapping is auditable against the regulation.
CN_CODE_TO_COMMODITY: dict[str, Commodity] = {
    # ----------------------------------------------------------------- cattle
    # Live bovine animals, bovine meat (fresh/chilled/frozen), and the raw and
    # tanned hides/leather chapters that Annex I brings in as cattle products.
    "0102": Commodity.cattle,  # Live bovine animals
    "0201": Commodity.cattle,  # Meat of bovine animals, fresh or chilled
    "0202": Commodity.cattle,  # Meat of bovine animals, frozen
    "4101": Commodity.cattle,  # Raw hides and skins of bovine animals
    "4104": Commodity.cattle,  # Tanned/crust hides and skins of bovine animals
    "4107": Commodity.cattle,  # Leather further prepared, of bovine animals
    # ------------------------------------------------------------------ cocoa
    # Cocoa beans through finished chocolate; Annex I lists the whole 18xx block
    # of cocoa products.
    "1801": Commodity.cocoa,  # Cocoa beans, whole or broken, raw or roasted
    "1802": Commodity.cocoa,  # Cocoa shells, husks, skins and other cocoa waste
    "1803": Commodity.cocoa,  # Cocoa paste, whether or not defatted
    "1804": Commodity.cocoa,  # Cocoa butter, fat and oil
    "1805": Commodity.cocoa,  # Cocoa powder, not containing added sugar
    "1806": Commodity.cocoa,  # Chocolate and other cocoa-containing preparations
    # ----------------------------------------------------------------- coffee
    # Coffee, roasted or not, decaffeinated or not, and husks/skins.
    "0901": Commodity.coffee,  # Coffee, whether or not roasted or decaffeinated
    # --------------------------------------------------------------- oil palm
    # Palm oil and its fractions, palm nuts/kernels, palm-kernel oil, the
    # oilcake residue, and glycerol/industrial fatty acids derived from palm.
    "1207.10": Commodity.oil_palm,  # Palm nuts and kernels
    "1511": Commodity.oil_palm,  # Palm oil and its fractions
    "1513": Commodity.oil_palm,  # Coconut/palm-kernel/babassu oil and fractions
    "2306.60": Commodity.oil_palm,  # Oilcake/residues from palm nuts or kernels
    "1516.20": Commodity.oil_palm,  # Hydrogenated vegetable fats/oils (palm)
    "1517": Commodity.oil_palm,  # Margarine; edible palm-oil mixtures/preparations
    # ------------------------------------------------------------------- soya
    # Soya beans, soya-bean oilcake, and soya-bean oil and its fractions.
    "1201": Commodity.soya,  # Soya beans, whether or not broken
    "1208.10": Commodity.soya,  # Flours and meals of soya beans
    "1507": Commodity.soya,  # Soya-bean oil and its fractions
    "2304": Commodity.soya,  # Oilcake and residues from extracting soya-bean oil
    # ----------------------------------------------------------------- rubber
    # Natural rubber (latex and primary forms), plates/sheets, and finished
    # articles such as tyres and inner tubes that Annex I lists as rubber.
    "4001": Commodity.rubber,  # Natural rubber, latex, in primary forms
    "4005": Commodity.rubber,  # Compounded rubber, unvulcanised, primary forms
    "4006": Commodity.rubber,  # Other unvulcanised rubber forms and articles
    "4007": Commodity.rubber,  # Vulcanised rubber thread and cord
    "4008": Commodity.rubber,  # Plates, sheets, strip, rods of vulcanised rubber
    "4010": Commodity.rubber,  # Conveyor/transmission belts of vulcanised rubber
    "4011": Commodity.rubber,  # New pneumatic tyres, of rubber
    "4012": Commodity.rubber,  # Retreaded/used pneumatic tyres; solid tyres
    "4013": Commodity.rubber,  # Inner tubes, of rubber
    # ------------------------------------------------------------------- wood
    # Chapter 44 (wood and articles of wood) plus the pulp/paper and printed
    # products Annex I reaches into other chapters for (47xx pulp, 48xx paper,
    # 49xx printed matter) and wooden furniture heading 9403.
    "4401": Commodity.wood,  # Fuel wood, chips, sawdust, wood pellets
    "4402": Commodity.wood,  # Wood charcoal
    "4403": Commodity.wood,  # Wood in the rough
    "4404": Commodity.wood,  # Hoopwood; split poles; wood roughly trimmed
    "4405": Commodity.wood,  # Wood wool; wood flour
    "4406": Commodity.wood,  # Railway or tramway sleepers of wood
    "4407": Commodity.wood,  # Wood sawn or chipped lengthwise, thickness > 6 mm
    "4408": Commodity.wood,  # Sheets for veneering / plywood, thickness <= 6 mm
    "4409": Commodity.wood,  # Wood continuously shaped along any edge/face
    "4410": Commodity.wood,  # Particle board, OSB and similar board of wood
    "4411": Commodity.wood,  # Fibreboard of wood or other ligneous materials
    "4412": Commodity.wood,  # Plywood, veneered panels and similar laminated wood
    "4413": Commodity.wood,  # Densified wood, in blocks, plates, strips, profiles
    "4414": Commodity.wood,  # Wooden frames for paintings, photographs, mirrors
    "4415": Commodity.wood,  # Packing cases, boxes, crates, pallets, of wood
    "4416": Commodity.wood,  # Casks, barrels, vats and other coopers' products
    "4417": Commodity.wood,  # Tools, tool handles, broom/brush bodies, of wood
    "4418": Commodity.wood,  # Builders' joinery and carpentry of wood
    "4419": Commodity.wood,  # Tableware and kitchenware, of wood
    "4420": Commodity.wood,  # Marquetry; wooden caskets; statuettes; furniture
    "4421": Commodity.wood,  # Other articles of wood
    "4701": Commodity.wood,  # Mechanical wood pulp
    "4702": Commodity.wood,  # Chemical wood pulp, dissolving grades
    "4703": Commodity.wood,  # Chemical wood pulp, soda or sulphate
    "4704": Commodity.wood,  # Chemical wood pulp, sulphite
    "4705": Commodity.wood,  # Semi-chemical wood pulp
    "4801": Commodity.wood,  # Newsprint, in rolls or sheets
    "4802": Commodity.wood,  # Uncoated paper for writing/printing/graphic use
    "4803": Commodity.wood,  # Toilet/tissue/towel stock paper, in rolls/sheets
    "4804": Commodity.wood,  # Uncoated kraft paper and paperboard
    "4805": Commodity.wood,  # Other uncoated paper and paperboard
    "4806": Commodity.wood,  # Vegetable parchment, greaseproof papers, glassine
    "4807": Commodity.wood,  # Composite paper and paperboard
    "4808": Commodity.wood,  # Corrugated / creped / crinkled paper and paperboard
    "4809": Commodity.wood,  # Carbon/self-copy and other copying/transfer paper
    "4810": Commodity.wood,  # Paper/paperboard coated with kaolin or inorganics
    "4811": Commodity.wood,  # Paper/paperboard, coated/impregnated/surface-worked
    "4812": Commodity.wood,  # Filter blocks, slabs and plates, of paper pulp
    "4813": Commodity.wood,  # Cigarette paper
    "4814": Commodity.wood,  # Wallpaper and similar wall coverings
    "4816": Commodity.wood,  # Carbon/copying paper (other than 4809), duplicators
    "4817": Commodity.wood,  # Envelopes, letter cards, plain postcards, of paper
    "4818": Commodity.wood,  # Toilet paper, tissues, towels, napkins, of paper
    "4819": Commodity.wood,  # Cartons, boxes, cases, bags of paper/paperboard
    "4820": Commodity.wood,  # Registers, notebooks, binders, forms, of paper
    "4821": Commodity.wood,  # Paper or paperboard labels of all kinds
    "4822": Commodity.wood,  # Bobbins, spools, cops and similar supports of pulp
    "4823": Commodity.wood,  # Other paper, paperboard, cellulose wadding articles
    "4901": Commodity.wood,  # Printed books, brochures, leaflets and similar
    "4902": Commodity.wood,  # Newspapers, journals and periodicals
    "4903": Commodity.wood,  # Children's picture, drawing or colouring books
    "4904": Commodity.wood,  # Music, printed or in manuscript
    "4905": Commodity.wood,  # Maps and hydrographic/similar charts, printed
    "4906": Commodity.wood,  # Plans/drawings for architecture/engineering, hand
    "4907": Commodity.wood,  # Unused stamps; banknotes; cheque forms; certificates
    "4908": Commodity.wood,  # Transfers (decalcomanias)
    "4909": Commodity.wood,  # Printed postcards; printed greeting/message cards
    "4910": Commodity.wood,  # Printed calendars of any kind
    "4911": Commodity.wood,  # Other printed matter, including pictures/photographs
    "9403.30": Commodity.wood,  # Wooden furniture of a kind used in offices
    "9403.40": Commodity.wood,  # Wooden furniture of a kind used in the kitchen
    "9403.50": Commodity.wood,  # Wooden furniture of a kind used in the bedroom
    "9403.60": Commodity.wood,  # Other wooden furniture
    "9406.10": Commodity.wood,  # Prefabricated buildings of wood
}


def _normalize_cn(cn_code: str) -> str:
    """Normalize a CN code to digits only (strip spaces, dots, and casing).

    CN codes are numeric; separators such as dots or spaces ("0901.21",
    "0901 21") are formatting only. A code carrying any non-digit, non-separator
    character is malformed and rejected loudly rather than silently cleaned.
    """
    stripped = cn_code.strip().replace(".", "").replace(" ", "")
    if not stripped:
        raise ValueError("CN code is empty")
    if not stripped.isdigit():
        raise ValueError(f"CN code is not numeric: {cn_code!r}")
    return stripped


# Table keys pre-normalized once, longest heading first so that a more specific
# subheading (e.g. "120810") is preferred over a shorter heading when both would
# prefix-match the declared code.
_NORMALIZED_TABLE: tuple[tuple[str, Commodity], ...] = tuple(
    sorted(
        ((_normalize_cn(code), commodity) for code, commodity in CN_CODE_TO_COMMODITY.items()),
        key=lambda item: len(item[0]),
        reverse=True,
    )
)


def matched_heading_for_cn(cn_code: str) -> str | None:
    """Return the normalized Annex I heading a declared CN code matches, or None.

    Prefix semantics: the declared code must START WITH a table heading (so
    "090121" matches "0901"). The most specific heading wins when several match.
    Returns ``None`` when the code is out of scope. Raises ``ValueError`` for a
    syntactically invalid (non-numeric/empty) code.
    """
    normalized = _normalize_cn(cn_code)
    for heading, _commodity in _NORMALIZED_TABLE:
        if normalized.startswith(heading):
            return heading
    return None


def commodity_for_cn(cn_code: str) -> Commodity | None:
    """Return the Annex I :class:`Commodity` for a CN code, or ``None``.

    Normalizes the code (strips spaces/dots) and prefix-matches it against the
    Annex I headings so a subheading such as "090111" resolves to coffee via the
    "0901" heading. Returns ``None`` when the code is out of scope. Raises
    ``ValueError`` for a syntactically invalid code (fail loud: a malformed code
    is a caller error, not "out of scope").
    """
    normalized = _normalize_cn(cn_code)
    for heading, commodity in _NORMALIZED_TABLE:
        if normalized.startswith(heading):
            return commodity
    return None
