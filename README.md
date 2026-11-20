# Zoho Webhook Mapper v2 — Quick Guide

You already know Mapper v1. What v2 changes: it maps **both sides** of each row — Parameter Name on the left, field dropdown on the right — in **MLS PDF order**. You create the rows with Zoho's Auto-Map button; the tool rewrites them in order. The tool never clicks Save; you review and save.

**Files you need** (from this GitHub repo, keep them in one folder): `app.py` and the JSON file for your form, e.g. `keys.json`. One form = one JSON — don't mix them up.

**One-time setup** (per computer):

```
python -m pip install playwright
```

---

## Run it

1. In Zoho Forms, open **Integrations → Webhooks → Payload Mapping** and remove any old rows first.
2. **Test on 3 rows:**

```
python app.py keys.json --limit 3 --url PASTE_YOUR_FORM_LINK_HERE
```

3. Chrome opens by itself. Log in to Zoho, open the Payload Mapping page, and click **Auto-Map Fields** yourself so there is one row per field. Then press **Enter** in the terminal. **Don't touch Chrome while it runs** — `[ok]` in the terminal means the row is done.
4. Check the 3 rows. If they look right, remove all rows again and run the **full file** — same command without `--limit 3`. Delete the leftover Auto-Map rows (listed in `extra_rows.txt`) yourself from the bottom up before saving.
5. Check the start, middle and end of the table, then click **Save** in Zoho yourself.

**Good to know**

- The standard fields are already in the JSON: **Property Type** and **Address** on top; **task_id, FormID, UniqueID, Date_Created, env, added_time, ip_address, house_id, live_key** at the bottom. If a JSON file ever misses them, the tool adds them itself — they are never mapped twice.
- `failed_rows.txt` lists rows the tool could not fill — fill those by hand. `extra_rows.txt` lists the leftover rows to delete before saving.
- **Never delete or share the `zoho_profile` folder** — it contains your Zoho login.
- `python is not recognized` → Python is not installed, ask Shreyan. Many `[FAIL]` rows → stop, don't save, ask Shreyan.

---

## Making the JSON for a new MLS — 3-step prompt pack

Use **Muse**, in **one chat**, sending the three messages below **one at a time**. Don't use a single all-in-one prompt — the output quality drops. You need the **MLS input sheet PDF(s)** (decides order + section names) and the **Zoho builder page PDF print** (decides the exact field names).

### Message 1 — attach the MLS PDF(s) only:

```
Read every page of the attached MLS input sheet PDF(s) carefully.
List EVERY section and EVERY field, in the exact order they appear on the PDF.
Fields only — do not list the options inside checkbox / radio / dropdown fields.
Number the fields continuously and give me the total count at the end.
Do not create any JSON yet. Just give me the list, section by section.
```

### Message 2 — attach the Zoho builder PDF print:

```
Now read the attached Zoho form builder PDF print.
List EVERY field label exactly as written in the builder, in order.
Mark each field that has an orange/yellow eye icon next to it as [hidden].
Mark signature fields as [signature].
Give me the total count at the end. Do not create any JSON yet.
```

### Message 3 — no attachments:

```
Now create the webhook mapping JSON from the two lists you just made.

Rules:
1. Order and section names come from the MLS list (Message 1). Go field by field in that exact order.
2. For each MLS field, use the matching field's EXACT label from the builder list (Message 2) as "zoho_label". If the wording is slightly different but it is clearly the same field, still match it.
3. "parameter_name" format: <Section>_<FieldName> in Title_Case_With_Underscores, for example General_Information_County. Keep the field's wording. Remove special characters. "#" becomes "Number". "Y/N" becomes "YN". Fractions in words (1/2 = Half, 1/4 = One_Quarter, 3/4 = Three_Quarter). Roman numerals become normal numbers (II = 2).
4. Skip a field only if it has no matching field in the builder list, or it is a signature field or the date-time field below a signature.
5. HIDDEN FIELDS: hidden fields are mapped like normal fields, at their PDF position. Do not skip a field because it is hidden. (Exception: the standard fields in rule 9.)
6. Never repeat a "parameter_name" or a "zoho_label". If two fields would get the same name, add a meaningful word (Details, Type, Features, Information). Never use _1, _2.
7. If the same MLS field is a different builder field per property type, include EACH type's version at that position. Example: "Property Sub Type" = Home Type (Residential), Multi-Family Sub-Type (Multi-Family), Land Sub-Type (Land).
8. LISTING DATES: "List Date" and "Expire Date" are called "Listing Date" and "Plan End Date" (parameter names end _Listing_Date / _Plan_End_Date), and ALWAYS add "Listing Term" (parameter _Listing_Term) between them.
9. STANDARD FIELDS: at the very top add "Property Type" (zoho_label "Property Type"), then "Address" (zoho_label "Address"). At the very bottom add, in this order: task_id, FormID (zoho_label "Unique ID"), UniqueID (zoho_label "Unique ID"), Date_Created (zoho_label "Date-Time"), env, added_time (zoho_label "Added Time"), ip_address (zoho_label "IP Address"), house_id, live_key. FormID and UniqueID both using "Unique ID" is the one allowed duplicate label.
10. CONVENIENCE RADIOS: if the builder list has a Yes/No radio that is NOT in the MLS list and other fields depend on it (example: a "Fireplace" radio above the fireplace detail fields), include it right before the first field that depends on it. ONLY if such a radio actually exists in the builder list — never invent one.
11. If I gave more than one property type, make ONE merged list: Residential order is the main order; fields unique to other types go right after the nearest related field.
12. Tables in the PDF (Rooms, Units) that Zoho implements with generic repeating fields: include those generic builder fields once, at the table's position.

13. OTHER ROOMS: the Zoho form has team-added "Other Room" fields that are NOT in the MLS PDF (Other Room 1, Other Room 2, and so on), and each one has Name, Dimensions and Level fields in the builder list. Include all of them — the room field itself first, then its Name, Dimensions and Level — placed immediately after the LAST field of the Rooms section.
Before you output, check yourself:
- First two entries are Property Type and Address; last entry is live_key.
- Listing Date, Listing Term, Plan End Date are together, in that order.
- No duplicate parameter names; no duplicate labels except Unique ID twice.
- Count your entries and tell me the count in one line BEFORE the JSON.

Output format: one line with the total count, then ONLY a JSON array like this — no explanation after it:
[
  {"parameter_name": "General_Information_County", "zoho_label": "County"},
  {"parameter_name": "General_Information_List_Price", "zoho_label": "List Price"}
]
If the JSON is too long for one reply, give it in parts, section by section, and I will say "continue" — do not skip or summarise any field.
```

**After you get the JSON:** save it as a `.json` file, run the tool with `--dry-run` and check the count matches what Muse claimed, then test with `--limit 3` as usual.

---

## Rules to remember

1. Always test with `--limit 3` first on a new form or new JSON.
2. Never click Save until you have checked the start, middle and end.
3. Don't edit the JSON file unless Shreyan asks you to.
