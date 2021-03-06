import re
import scrapelib
from collections import defaultdict
from pytz import timezone
from datetime import datetime
from pupa.scrape import Scraper, Bill, VoteEvent
from openstates.utils import LXMLMixin
from pupa.utils.generic import convert_pdf
import pytz
import math

central = pytz.timezone("US/Central")


def chamber_abbr(chamber):
    if chamber == "upper":
        return "S"
    else:
        return "H"


def session_url(session):
    return "https://apps.legislature.ky.gov/record/%s/" % session[2:]


class KYBillScraper(Scraper, LXMLMixin):
    _TZ = timezone("America/Kentucky/Louisville")
    _subjects = defaultdict(list)
    _is_post_2016 = False

    _action_classifiers = [
        ("introduced in", "introduction"),
        ("signed by Governor", ["executive-signature"]),
        ("vetoed", "executive-veto"),
        (r"^to [A-Z]", "referral-committee"),
        (" to [A-Z]", "referral-committee"),
        ("reported favorably", "committee-passage"),
        ("adopted by voice vote", "passage"),
        ("3rd reading, passed", ["reading-3", "passage"]),
        ("1st reading", "reading-1"),
        ("2nd reading", "reading-2"),
        ("3rd reading", "reading-3"),
        ("passed", "passage"),
        ("delivered to secretary of state", "became-law"),
        ("veto overridden", "veto-override-passage"),
        ("adopted by voice vote", "passage"),
        (
            r"floor amendments?( \([a-z\d\-]+\))*" r"( and \([a-z\d\-]+\))? filed",
            "amendment-introduction",
        ),
    ]

    def classify_action(self, action):
        for regex, classification in self._action_classifiers:
            if re.match(regex, action):
                return classification
        return None

    def scrape(self, session=None, chamber=None):
        if not session:
            session = self.latest_session()
            self.info("no session specified, using %s", session)
        # Bill page markup changed starting with the 2016 regular session.
        # kinda gross
        if int(session[0:4]) >= 2016:
            self._is_post_2016 = True

        # self.scrape_subjects(session)
        chambers = [chamber] if chamber else ["upper", "lower"]
        for chamber in chambers:
            yield from self.scrape_session(chamber, session)

    def scrape_session(self, chamber, session):
        chamber_map = {"upper": "senate", "lower": "house"}
        bill_url = session_url(session) + "%s_bills.html" % chamber_map[chamber]
        yield from self.scrape_bill_list(chamber, session, bill_url)

        resolution_url = (
            session_url(session) + "%s_resolutions.html" % chamber_map[chamber]
        )
        yield from self.scrape_bill_list(chamber, session, resolution_url)

    def scrape_bill_list(self, chamber, session, url):
        bill_abbr = None
        page = self.lxmlize(url)

        for link in page.xpath("//div[contains(@class,'container')]/p/a"):
            if re.search(r"\d{1,4}\.htm", link.attrib.get("href", "")):
                bill_id = link.text
                match = re.match(
                    r".*\/([a-z]+)([\d+])\.html", link.attrib.get("href", "")
                )
                if match:
                    bill_abbr = match.group(1)
                    bill_id = bill_abbr.upper() + bill_id.replace(" ", "")
                else:
                    bill_id = bill_abbr + bill_id

                yield from self.parse_bill(
                    chamber, session, bill_id, link.attrib["href"]
                )

    def parse_actions(self, page, bill, chamber):
        # //div[preceding-sibling::a[@id="actions"]]
        action_rows = page.xpath(
            '//div[preceding-sibling::a[@id="actions"]][1]/table[1]/tbody/tr'
        )
        for row in action_rows:
            action_date = row.xpath("th[1]/text()")[0].strip()

            action_date = datetime.strptime(action_date, "%m/%d/%y")
            action_date = self._TZ.localize(action_date)

            action_texts = row.xpath("td[1]/ul/li/text() | td[1]/ul/li/strong/text()")

            for action_text in action_texts:
                action_text = action_text.strip()
                if action_text.endswith("House") or action_text.endswith("(H)"):
                    actor = "lower"
                elif action_text.endswith("Senate") or action_text.endswith("(S)"):
                    actor = "upper"
                else:
                    actor = chamber

                classifications = self.classify_action(action_text)
                bill.add_action(
                    action_text,
                    action_date,
                    chamber=actor,
                    classification=classifications,
                )

    # Get the field to the right for a given table header
    def parse_bill_field(self, page, header):
        xpath_expr = '//tr[th[text()="{}"]]/td[1]'.format(header)
        return page.xpath(xpath_expr)[0]

    def parse_bill(self, chamber, session, bill_id, url):
        try:
            page = self.lxmlize(url)
        except scrapelib.HTTPError as e:
            self.logger.warning(e)
            return

        last_action = self.parse_bill_field(page, "Last Action").xpath("text()")[0]
        if "WITHDRAWN" in last_action.upper():
            self.info("{} Withdrawn, skipping".format(bill_id))
            return

        version = self.parse_bill_field(page, "Bill Documents")
        source_url = version.xpath("a[1]/@href")[0]
        version_title = version.xpath("a[1]/text()")[0].strip()

        if version is None:
            # Bill withdrawn
            self.logger.warning("Bill withdrawn.")
            return
        else:
            if source_url.endswith(".doc"):
                mimetype = "application/msword"
            elif source_url.endswith(".pdf"):
                mimetype = "application/pdf"

        title = self.parse_bill_field(page, "Title").text_content()

        # actions = self.get_nodes(
        #     page,
        #     '//div[@class="StandardText leftDivMargin"]/'
        #     'div[@class="StandardText"][last()]//text()[normalize-space()]')

        if "CR" in bill_id:
            bill_type = "concurrent resolution"
        elif "JR" in bill_id:
            bill_type = "joint resolution"
        elif "R" in bill_id:
            bill_type = "resolution"
        else:
            bill_type = "bill"

        bill = Bill(
            bill_id,
            legislative_session=session,
            chamber=chamber,
            title=title,
            classification=bill_type,
        )
        bill.subject = self._subjects[bill_id]
        bill.add_source(url)

        bill.add_version_link(version_title, source_url, media_type=mimetype)

        self.parse_actions(page, bill, chamber)
        self.parse_subjects(page, bill)

        # LM is "Locally Mandated fiscal impact"
        fiscal_notes = page.xpath('//a[contains(@href, "/LM.pdf")]')
        for fiscal_note in fiscal_notes:
            source_url = fiscal_note.attrib["href"]
            if source_url.endswith(".doc"):
                mimetype = "application/msword"
            elif source_url.endswith(".pdf"):
                mimetype = "application/pdf"

            bill.add_document_link("Fiscal Note", source_url, media_type=mimetype)

        for link in page.xpath("//td/span/a[contains(@href, 'Legislator-Profile')]"):
            bill.add_sponsorship(
                link.text.strip(),
                classification="primary",
                entity_type="person",
                primary=True,
            )

        if page.xpath("//th[contains(text(),'Votes')]"):
            vote_url = page.xpath("//a[contains(text(),'Vote History')]/@href")[0]
            yield from self.scrape_votes(vote_url, bill, chamber)

        bdr_no = self.parse_bill_field(page, "Bill Request Number")
        if bdr_no.xpath("text()"):
            bdr = bdr_no.xpath("text()")[0].strip()
            bill.extras["BDR"] = bdr

        yield bill

    def scrape_votes(self, vote_url, bill, chamber):
        # Grabs text from pdf
        pdflines = [
            line.decode("utf-8") for line in convert_pdf(vote_url, "text").splitlines()
        ]
        vote_date = 0
        voters = defaultdict(list)
        for x in range(len(pdflines)):
            line = pdflines[x]
            if re.search(r"(\d+/\d+/\d+)", line):
                initial_date = line.strip()
            if ("AM" in line) or ("PM" in line):
                split_l = line.split()
                for y in split_l:
                    if ":" in y:
                        time_location = split_l.index(y)
                        motion = " ".join(split_l[0:time_location])
                        time = split_l[time_location:]
                        if len(time) > 0:
                            time = "".join(time)
                        dt = initial_date + " " + time
                        dt = datetime.strptime(dt, "%m/%d/%Y %I:%M:%S%p")
                        vote_date = central.localize(dt)
                        vote_date = vote_date.isoformat()
                        # In rare case that no motion is provided
                        if len(motion) < 1:
                            motion = "No Motion Provided"
            if "YEAS:" in line:
                yeas = int(line.split()[-1])
            if "NAYS:" in line:
                nays = int(line.split()[-1])
            if "ABSTAINED:" in line:
                abstained = int(line.split()[-1])
            if "PASSES:" in line:
                abstained = int(line.split()[-1])
            if "NOT VOTING:" in line:
                not_voting = int(line.split()[-1])

            if "YEAS :" in line:
                y = 0
                next_line = pdflines[x + y]
                while "NAYS : " not in next_line:
                    next_line = next_line.split("  ")
                    if next_line and ("YEAS" not in next_line):
                        for v in next_line:
                            if v and "YEAS" not in v:
                                voters["yes"].append(v.strip())
                    next_line = pdflines[x + y]
                    y += 1
            if line and "NAYS :" in line:
                y = 0
                next_line = 0
                next_line = pdflines[x + y]
                while ("ABSTAINED : " not in next_line) and (
                    "PASSES :" not in next_line
                ):
                    next_line = next_line.split("  ")
                    if next_line and "NAYS" not in next_line:
                        for v in next_line:
                            if v and "NAYS" not in v:
                                voters["no"].append(v.strip())
                    next_line = pdflines[x + y]
                    y += 1

            if line and ("ABSTAINED :" in line or "PASSES :" in line):
                y = 2
                next_line = 0
                next_line = pdflines[x + y]
                while "NOT VOTING :" not in next_line:
                    next_line = next_line.split("  ")
                    if next_line and (
                        "ABSTAINED" not in next_line or "PASSES" not in next_line
                    ):
                        for v in next_line:
                            if v:
                                voters["abstain"].append(v.strip())
                    next_line = pdflines[x + y]
                    y += 1

            if line and "NOT VOTING : " in line:
                lines_to_go_through = math.ceil(not_voting / len(line.split()))
                next_line = pdflines[x]
                for y in range(lines_to_go_through):
                    next_line = pdflines[x + y + 2].split("  ")
                    for v in next_line:
                        if v:
                            voters["not voting"].append(v.strip())
                if yeas > (nays + abstained + not_voting):
                    passed = True
                else:
                    passed = False

                ve = VoteEvent(
                    chamber=chamber,
                    start_date=vote_date,
                    motion_text=motion,
                    result="pass" if passed else "fail",
                    classification="bill",
                    bill=bill,
                )
                ve.add_source(vote_url)
                for how_voted, how_voted_voters in voters.items():
                    for voter in how_voted_voters:
                        if len(voter) > 0:
                            ve.vote(how_voted, voter)
                # Resets voters dictionary before going onto next page in pdf
                voters = defaultdict(list)
                yield ve

    def parse_subjects(self, page, bill):
        subject_div = self.parse_bill_field(page, "Index Headings of Original Version")
        subjects = subject_div.xpath("a/text()")
        seen_subjects = []
        for subject in subjects:
            if subject not in seen_subjects:
                bill.add_subject(subject.strip())
                seen_subjects.append(subject)
