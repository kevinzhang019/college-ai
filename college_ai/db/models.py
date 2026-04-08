"""SQLAlchemy ORM models for admissions data."""

from sqlalchemy import Column, Integer, Float, Text, ForeignKey
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


class School(Base):
    __tablename__ = "schools"

    id = Column(Integer, primary_key=True)  # College Scorecard UNITID
    name = Column(Text, nullable=False)
    city = Column(Text)
    state = Column(Text)
    ownership = Column(Integer)  # 1=public, 2=private nonprofit, 3=for-profit

    # identity_ — school metadata
    identity_alias = Column(Text)           # e.g. "MIT, M.I.T."
    identity_url = Column(Text)             # school website
    identity_locale = Column(Integer)       # LOCALE code (11=city-large, 12=city-midsize, etc.)
    identity_carnegie_basic = Column(Integer)
    identity_religious_affiliation = Column(Integer)

    # admissions_ — selectivity & test scores
    admissions_rate = Column(Float)
    admissions_sat_avg = Column(Float)
    admissions_sat_25 = Column(Float)
    admissions_sat_75 = Column(Float)
    admissions_act_25 = Column(Float)
    admissions_act_75 = Column(Float)
    admissions_test_requirements = Column(Integer)  # 1=required 2=recommended 3=neither 5=flexible

    # student_ — enrollment, demographics, retention
    student_size = Column(Integer)
    student_retention_rate = Column(Float)
    student_faculty_ratio = Column(Float)
    student_avg_age_entry = Column(Integer)
    student_pct_men = Column(Float)
    student_pct_women = Column(Float)
    student_part_time_share = Column(Float)
    student_pct_white = Column(Float)
    student_pct_black = Column(Float)
    student_pct_hispanic = Column(Float)
    student_pct_asian = Column(Float)
    student_pct_first_gen = Column(Float)

    # cost_ — tuition, net price, cost of attendance
    cost_tuition_in_state = Column(Integer)
    cost_tuition_out_of_state = Column(Integer)
    cost_attendance = Column(Integer)        # total COA (academic year)
    cost_avg_net_price = Column(Integer)     # avg net price after aid
    cost_booksupply = Column(Integer)
    cost_net_price_0_30k = Column(Integer)   # net price by family income bracket
    cost_net_price_30_48k = Column(Integer)
    cost_net_price_48_75k = Column(Integer)
    cost_net_price_75_110k = Column(Integer)
    cost_net_price_110k_plus = Column(Integer)

    # aid_ — financial aid & debt
    aid_pell_grant_rate = Column(Float)
    aid_federal_loan_rate = Column(Float)
    aid_median_debt = Column(Float)
    aid_cumulative_debt_25th = Column(Float)
    aid_cumulative_debt_75th = Column(Float)

    # outcome_ — graduation & earnings
    outcome_graduation_rate = Column(Float)
    outcome_median_earnings_10yr = Column(Integer)

    # institution_ — resources & faculty
    institution_endowment = Column(Integer)
    institution_faculty_salary = Column(Integer)
    institution_ft_faculty_rate = Column(Float)
    institution_instructional_spend_per_fte = Column(Integer)

    updated_at = Column(Text)

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


