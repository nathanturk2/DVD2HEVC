"""DVD ISO 639-1 metadata to Blu-ray ISO 639-2/T track languages.

Unlisted/undefined DVD codes use 'und'; navigation still retains the original
two-letter code. No language is guessed from a track's position.
"""
ISO3=dict(pair.split(':') for pair in (
    'ar:ara bg:bul ca:cat cs:ces da:dan de:deu el:ell en:eng es:spa et:est '
    'fa:fas fi:fin fr:fra he:heb hi:hin hr:hrv hu:hun id:ind is:isl it:ita '
    'ja:jpn ko:kor lt:lit lv:lav ms:msa nl:nld no:nor pl:pol pt:por ro:ron '
    'ru:rus sk:slk sl:slv sr:srp sv:swe ta:tam te:tel th:tha tr:tur uk:ukr '
    'ur:urd vi:vie zh:zho iw:heb in:ind ji:yid').split())

def audio_languages(graph,refs):
    choices={}
    for pgc,cell in refs:
        domain=graph['domains'][('T:' if pgc['domain']=='title' else 'M:')+str(pgc['vts'])]
        for track in domain['audio']:
            logical=track['ordinal'];control=pgc['audio'][logical]
            if control&0x8000:
                physical=(control>>8)&7;choices.setdefault(physical,set()).add(ISO3.get(track['language'],'und'))
    return {physical:next(iter(languages)) if len(languages)==1 else 'und' for physical,languages in choices.items()}
