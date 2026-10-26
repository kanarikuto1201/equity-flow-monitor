"""読み取り処理のテスト。本物の書類と同じ構造の小さな見本で確かめる。"""
from efm.us.edgar import IndexEntry, parse_form_index, extract_xml
from efm.us.insiders import parse_form4, parse_form144
from efm.us.offerings import prospectus_text, _EQUITY_HINT

INDEX = """Description:           Daily Index of EDGAR Dissemination Feed by Form Type
Last Data Received:    Oct 23, 2026
Comments:              webmaster@sec.gov

Form Type   Company Name                                                  CIK         Date Filed  File Name
---------------------------------------------------------------------------------------------------------------------------------------------
10-K/A      SOME COMPANY INC                                              1234567     20261023    edgar/data/1234567/0001234567-26-000001.txt
4           EXAMPLE CORP                                                  1111111     20261023    edgar/data/1111111/0000950170-26-000123.txt
4           DOE JANE                                                      2222222     20261023    edgar/data/1111111/0000950170-26-000123.txt
424B4       NEW AI CO                                                     3333333     20261023    edgar/data/3333333/0001193125-26-000999.txt
"""

FORM4 = b"""<?xml version="1.0"?>
<ownershipDocument>
  <issuer><issuerCik>0001111111</issuerCik><issuerName>Example Corp</issuerName><issuerTradingSymbol>exm</issuerTradingSymbol></issuer>
  <reportingOwner>
    <reportingOwnerId><rptOwnerCik>0002222222</rptOwnerCik><rptOwnerName>Doe Jane</rptOwnerName></reportingOwnerId>
    <reportingOwnerRelationship><isDirector>0</isDirector><isOfficer>1</isOfficer><isTenPercentOwner>0</isTenPercentOwner></reportingOwnerRelationship>
  </reportingOwner>
  <aff10b5One>1</aff10b5One>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <transactionDate><value>2026-10-21</value></transactionDate>
      <transactionCoding><transactionFormType>4</transactionFormType><transactionCode>S</transactionCode></transactionCoding>
      <transactionAmounts><transactionShares><value>1,000</value></transactionShares><transactionPricePerShare><value>150.25</value><footnoteId id="F1"/></transactionPricePerShare><transactionAcquiredDisposedCode><value>D</value></transactionAcquiredDisposedCode></transactionAmounts>
    </nonDerivativeTransaction>
    <nonDerivativeTransaction>
      <transactionDate><value>2026-10-21</value></transactionDate>
      <transactionCoding><transactionCode>F</transactionCode></transactionCoding>
      <transactionAmounts><transactionShares><value>300</value></transactionShares><transactionPricePerShare><value>150</value></transactionPricePerShare></transactionAmounts>
    </nonDerivativeTransaction>
    <nonDerivativeTransaction>
      <transactionDate><value>2026-10-22</value></transactionDate>
      <transactionCoding><transactionCode>P</transactionCode></transactionCoding>
      <transactionAmounts><transactionShares><value>200</value></transactionShares><transactionPricePerShare><value>149</value></transactionPricePerShare></transactionAmounts>
    </nonDerivativeTransaction>
  </nonDerivativeTable>
</ownershipDocument>"""

FORM144 = b"""<?xml version="1.0"?>
<edgarSubmission xmlns="http://www.sec.gov/edgar/ownership">
  <formData>
    <issuerInfo>
      <issuerCik>0001111111</issuerCik><issuerName>Example Corp</issuerName>
      <nameOfPersonForWhoseAccountTheSecuritiesAreToBeSold>Big Venture Fund</nameOfPersonForWhoseAccountTheSecuritiesAreToBeSold>
      <relationshipsToIssuer><relationshipToIssuer>10% Owner</relationshipToIssuer></relationshipsToIssuer>
    </issuerInfo>
    <securitiesInformation>
      <securitiesClassTitle>Common</securitiesClassTitle>
      <noOfUnitsSold>50000</noOfUnitsSold>
      <aggregateMarketValue>7500000</aggregateMarketValue>
      <approxSaleDate>10/24/2026</approxSaleDate>
    </securitiesInformation>
  </formData>
</edgarSubmission>"""


def entry(form="4"):
    return IndexEntry(form, "EXAMPLE CORP", "1111111", "20261023", "edgar/data/1111111/0000950170-26-000123.txt")


def test_index_parses_and_dedupes():
    rows = parse_form_index(INDEX)
    forms = [r.form for r in rows]
    assert forms == ["10-K/A", "4", "424B4"]  # 同じForm 4が2行あっても1件
    assert rows[1].company == "EXAMPLE CORP"
    assert rows[1].accession == "0000950170-26-000123"


def test_extract_xml():
    sub = b"<SEC-DOCUMENT>\n<DOCUMENT>\n<TYPE>4\n<TEXT>\n<XML>\n" + FORM4 + b"\n</XML>\n</TEXT>\n</DOCUMENT>"
    assert extract_xml(sub).startswith(b"<?xml")


def test_form4_keeps_only_market_trades():
    trades = parse_form4(FORM4, entry())
    assert [t.code for t in trades] == ["S", "P"]  # Fは除外
    sell = trades[0]
    assert sell.ticker == "EXM" and sell.shares == 1000 and sell.value_usd == 150250.0
    assert sell.is_officer and sell.plan_10b5_1


def test_form144_with_namespace():
    notices = parse_form144(FORM144, entry("144"))
    assert len(notices) == 1
    n = notices[0]
    assert n.market_value_usd == 7_500_000 and n.shares == 50_000
    assert n.seller == "Big Venture Fund" and n.relationship == "10% Owner"


def test_prospectus_text_and_equity_hint():
    raw = (b"<SEC-DOCUMENT><DOCUMENT><TYPE>424B4<TEXT><html><body><p>10,000,000 Shares</p>"
           b"<p>New AI Co is offering shares of Class A common stock.</p><p>The Offering</p><p>Price $20.00</p>"
           b"</body></html></TEXT></DOCUMENT><DOCUMENT><TYPE>EX-1.1<TEXT>exhibit</TEXT></DOCUMENT>")
    text = prospectus_text(raw)
    assert "Class A common stock" in text and "exhibit" not in text
    assert _EQUITY_HINT.search(text)
    assert not _EQUITY_HINT.search("$500,000,000 4.25% Senior Notes due 2031")
