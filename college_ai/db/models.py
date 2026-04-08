"""SQLAlchemy ORM models for admissions data."""

from sqlalchemy import Column, Integer, Float, Text, ForeignKey
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


class School(Base):
    """A U.S. college/university sourced from the College Scorecard API.

    All data columns are prefixed by category for programmatic grouping:
        identity_   — school metadata (aliases, URL, classification codes)
        admissions_ — selectivity metrics and standardized test scores
        student_    — enrollment size, demographics, retention
        cost_       — tuition, total cost of attendance, net price by income
        aid_        — financial aid rates and student debt
        outcome_    — graduation rate and post-graduation earnings
        institution_ — endowment, faculty, and per-student spending

    All Float columns storing rates/percentages are 0.0–1.0 (not 0–100).
    All Integer dollar amounts are in nominal USD.
    """
    __tablename__ = "schools"

    id = Column(Integer, primary_key=True)  # IPEDS UNITID — unique federal school identifier
    name = Column(Text, nullable=False)     # Official school name (campus suffixes stripped)
    city = Column(Text)                     # City of main campus
    state = Column(Text)                    # Two-letter state abbreviation (e.g. "MA")
    ownership = Column(Integer)             # 1=public, 2=private nonprofit, 3=private for-profit

    # ── identity_ — school metadata & classification codes ────────────────
    identity_alias = Column(Text)           # Comma-separated alternative names (e.g. "MIT, M.I.T.")
    identity_url = Column(Text)             # School website URL (e.g. "web.mit.edu/")
    identity_locale = Column(Integer)       # NCES locale code:
        # 11=city-large (pop≥250k), 12=city-midsize (100-250k), 13=city-small (<100k)
        # 21=suburb-large, 22=suburb-midsize, 23=suburb-small
        # 31=town-fringe, 32=town-distant, 33=town-remote
        # 41=rural-fringe, 42=rural-distant, 43=rural-remote
    identity_carnegie_basic = Column(Integer)  # Carnegie Classification of Institutions:
        # 15=doctoral/very high research, 16=doctoral/high research,
        # 17=doctoral/professional, 18=master's-large, 19=master's-medium,
        # 20=master's-small, 21=baccalaureate-arts&sciences, 22=baccalaureate-diverse,
        # 23=baccalaureate/associate's, 24-33=associate's subtypes,
        # -2=not applicable, 0=not classified
    identity_religious_affiliation = Column(Integer)  # IPEDS religious code:
        # -1=not applicable (secular), 22=American Baptist, 24=African Methodist Episcopal,
        # 27=Baptist, 28=Southern Baptist, 30=Roman Catholic, 33=Christian,
        # 34=Church of God, 47=Episcopal, 48=Evangelical, 51=Friends (Quaker),
        # 52=Interdenominational, 54=Baptist (other), 55=Christian Methodist Episcopal,
        # 57=Lutheran, 58=Church of Christ, 59=Mennonite, 60=Methodist,
        # 61=United Methodist, 64=Nazarene, 66=Presbyterian, 67=Protestant,
        # 68=Reformed, 69=Seventh Day Adventist, 71=Methodist (other),
        # 73=Nondenominational, 74=Churches of Christ, 75=Southern Baptist (other),
        # 80=Jewish, 84=Presbyterian (other), 87=Assemblies of God,
        # 88=Brethren, 91=Congregational, 92=Evangelical Lutheran,
        # 94=Church of Latter-day Saints, 95=Adventist, 97=Lutheran (other),
        # 99=other, 100=Interdenominational (other), 101=Muslim, 102=Plymouth Brethren,
        # 103=Pentecostal, 105=Wesleyan, 106=Greek Orthodox, 107=Russian Orthodox,
        # 108=Unitarian

    identity_acceptance_rate = Column(Float)  # Overall acceptance rate (0.0–1.0, e.g. 0.05 = 5%)

    # ── admissions_ — selectivity & standardized test scores ──────────────
    admissions_sat_avg = Column(Float)      # Average SAT total score (400–1600, composite R+M)
    admissions_sat_25 = Column(Float)       # SAT 25th percentile composite (reading + math)
    admissions_sat_75 = Column(Float)       # SAT 75th percentile composite (reading + math)
    admissions_act_25 = Column(Float)       # ACT 25th percentile cumulative score (1–36)
    admissions_act_75 = Column(Float)       # ACT 75th percentile cumulative score (1–36)
    admissions_test_requirements = Column(Integer)  # Test policy:
        # 1=required, 2=recommended, 3=neither required nor recommended,
        # 4=do not know, 5=considered but not required (test-flexible)

    # ── student_ — enrollment, demographics, retention ────────────────────
    student_size = Column(Integer)          # Total undergraduate enrollment headcount
    student_retention_rate = Column(Float)  # First-time full-time 4-year retention rate (0.0–1.0)
    student_faculty_ratio = Column(Float)   # Students per faculty member (e.g. 5.0 means 5:1)
    student_avg_age_entry = Column(Integer) # Average age of entering students
    student_pct_men = Column(Float)         # Share of undergraduate enrollment that is male (0.0–1.0)
    student_pct_women = Column(Float)       # Share of undergraduate enrollment that is female (0.0–1.0)
    student_part_time_share = Column(Float) # Share of undergrads enrolled part-time (0.0–1.0)
    student_pct_white = Column(Float)       # Share of undergrads identifying as White (0.0–1.0)
    student_pct_black = Column(Float)       # Share of undergrads identifying as Black (0.0–1.0)
    student_pct_hispanic = Column(Float)    # Share of undergrads identifying as Hispanic (0.0–1.0)
    student_pct_asian = Column(Float)       # Share of undergrads identifying as Asian (0.0–1.0)
    student_pct_first_gen = Column(Float)   # Share of undergrads who are first-generation (0.0–1.0)

    # ── cost_ — tuition, total COA, net price by family income ────────────
    cost_tuition_in_state = Column(Integer)      # Published in-state tuition & fees (USD/year)
    cost_tuition_out_of_state = Column(Integer)  # Published out-of-state tuition & fees (USD/year)
    cost_attendance = Column(Integer)            # Total cost of attendance for academic year (USD)
    cost_avg_net_price = Column(Integer)         # Average annual net price after all grant/scholarship aid (USD)
    cost_booksupply = Column(Integer)            # Estimated annual books & supplies cost (USD)
    cost_net_price_0_30k = Column(Integer)       # Avg net price for families earning $0–$30k (USD)
    cost_net_price_30_48k = Column(Integer)      # Avg net price for families earning $30k–$48k (USD)
    cost_net_price_48_75k = Column(Integer)      # Avg net price for families earning $48k–$75k (USD)
    cost_net_price_75_110k = Column(Integer)     # Avg net price for families earning $75k–$110k (USD)
    cost_net_price_110k_plus = Column(Integer)   # Avg net price for families earning $110k+ (USD)

    # ── aid_ — financial aid rates & student debt ─────────────────────────
    aid_pell_grant_rate = Column(Float)          # Share of undergrads receiving Pell grants (0.0–1.0)
    aid_federal_loan_rate = Column(Float)        # Share of undergrads receiving federal loans (0.0–1.0)
    aid_median_debt = Column(Float)              # Median debt at graduation for completers (USD)
    aid_cumulative_debt_25th = Column(Float)     # 25th percentile of cumulative debt at graduation (USD)
    aid_cumulative_debt_75th = Column(Float)     # 75th percentile of cumulative debt at graduation (USD)

    # ── outcome_ — graduation & post-graduation earnings ──────────────────
    outcome_graduation_rate = Column(Float)      # IPEDS 150% time completion rate (0.0–1.0)
    outcome_median_earnings_10yr = Column(Integer)  # Median earnings 10 years after entry (USD/year)

    # ── institution_ — endowment, faculty quality & per-student spending ──
    institution_endowment = Column(Integer)      # End-of-year endowment value (USD, e.g. 24_572_716_000)
    institution_faculty_salary = Column(Integer)  # Average monthly faculty salary (USD/month)
    institution_ft_faculty_rate = Column(Float)  # Share of faculty that is full-time (0.0–1.0)
    institution_instructional_spend_per_fte = Column(Integer)  # Instructional expenditure per FTE student (USD/year)

    updated_at = Column(Text)  # ISO 8601 UTC timestamp of last Scorecard sync

    datapoints = relationship("ApplicantDatapoint", back_populates="school")
    niche_grade = relationship("NicheGrade", back_populates="school", uselist=False)


class ApplicantDatapoint(Base):
    __tablename__ = "applicant_datapoints"

    id = Column(Integer, primary_key=True, autoincrement=True)
    school_id = Column(Integer, ForeignKey("schools.id"), nullable=False)
    source = Column(Text, nullable=False)  # 'niche'
    gpa = Column(Float)
    sat_score = Column(Float)
    act_score = Column(Float)
    outcome = Column(Text, nullable=False)  # 'accepted' | 'rejected' | 'waitlisted'
    residency = Column(Text)        # 'inState' | 'outOfState'
    major = Column(Text)            # intended major / field of study
    scraped_at = Column(Text)

    school = relationship("School", back_populates="datapoints")


class NicheGrade(Base):
    __tablename__ = "niche_grades"

    school_id = Column(Integer, ForeignKey("schools.id"), primary_key=True)

    # Overall Niche grade and rank
    overall_grade = Column(Text)         # e.g. "A+", "A", "B+"
    niche_rank = Column(Integer)         # Overall national rank (e.g. 3)

    # Category letter grades
    academics = Column(Text)
    value = Column(Text)
    diversity = Column(Text)
    campus = Column(Text)
    athletics = Column(Text)
    party_scene = Column(Text)
    professors = Column(Text)
    location = Column(Text)
    dorms = Column(Text)
    food = Column(Text)
    student_life = Column(Text)
    safety = Column(Text)

    # Quantitative stats from Niche
    acceptance_rate_niche = Column(Float)       # e.g. 0.04
    avg_annual_cost = Column(Integer)           # net price in USD
    graduation_rate_niche = Column(Float)       # e.g. 0.95
    student_faculty_ratio_niche = Column(Float) # e.g. 5.0
    setting = Column(Text)                      # City / Suburb / Town / Rural
    religious_affiliation = Column(Text)        # e.g. "Catholic", "None"
    pct_students_on_campus = Column(Float)      # e.g. 0.93
    pct_greek_life = Column(Float)              # e.g. 0.15
    avg_rating = Column(Float)                  # Niche user star rating 1-5
    review_count = Column(Integer)

    no_data = Column(Integer, default=0)        # 1 = confirmed no Niche data for this school

    updated_at = Column(Text)

    school = relationship("School", back_populates="niche_grade")


