"""The base scraper class to be used for all web-scraping emitting Job objects
"""
import logging
import os
import random
import sys
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor, as_completed
from multiprocessing import Lock, Manager
from time import sleep, time
from typing import Any, Dict, List, Optional, Tuple, Union

from bs4 import BeautifulSoup
from requests import Session
from requests.adapters import HTTPAdapter
from tqdm import tqdm
from urllib3.util import Retry

from jobfunnel.backend import Job, JobStatus
from jobfunnel.backend.tools import Logger
from jobfunnel.backend.tools.delay import calculate_delays
from jobfunnel.backend.tools.filters import JobFilter
from jobfunnel.resources import (MAX_CPU_WORKERS, USER_AGENT_LIST, JobField,
                                 Locale)

if False:  # or typing.TYPE_CHECKING  if python3.5.3+
    from jobfunnel.config import JobFunnelConfigManager


class BaseScraper(ABC, Logger):
    """Base scraper object, for scraping and filtering Jobs from a provider
    """

    def __init__(self, session: Session, config: 'JobFunnelConfigManager',
                 job_filter: JobFilter) -> None:
        """Init

        Args:
            session (Session): session object used to make post and get requests
            config (JobFunnelConfigManager): config containing all needed paths,
                search proxy, delaying and other metadata.
            job_filter (JobFilter): object for filtering incoming jobs using
                various internal filters, including a content-matching tool.
                NOTE: this runs-on-the-fly as well, and preempts un-promising
                job scrapes to minimize session() usage.

        Raises:
            ValueError: if no Locale is configured in the JobFunnelConfigManager
        """
        # Inits
        super().__init__(
            level=config.log_level,
            file_path=config.log_file,
        )
        self.job_filter=job_filter
        self.session=session
        self.config=config
        if self.headers:
            self.session.headers.update(self.headers)

        # Elongate the retries TODO: make configurable
        retry = Retry(connect=3, backoff_factor=0.5)
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount('http://', adapter)
        self.session.mount('https://', adapter)

        # Ensure that the locale we want to use matches the locale that the
        # scraper was written to scrape in:
        if self.config.search_config.locale != self.locale:
            raise ValueError(
                f"Attempting to use scraper designed for {self.locale.name} "
                "when config indicates user is searching with "
                f"{self.config.search_config.locale.name}"
            )

        # Ensure our properties satisfy constraints
        self._validate_get_set()
        self.thread_manager = Manager()

        # Construct actions list which respects priority for scraping Jobs
        self._actions_list = [(True, f) for f in self.job_get_fields]
        self._actions_list += [(False, f) for f in self.job_set_fields if f
                               in self.high_priority_get_set_fields]
        self._actions_list += [(False, f) for f in self.job_set_fields if f not
                               in self.high_priority_get_set_fields]

    @property
    def user_agent(self) -> str:
        """Get a randomized user agent for this scraper
        """
        return random.choice(USER_AGENT_LIST)

    @property
    def job_init_kwargs(self) -> Dict[JobField, Any]:
        """This is a helper property that stores a Dict of JobField : value that
        we set defaults for when scraping. If the scraper fails to get/set these
        we can fail back to the empty value from here.

        i.e. JobField.POST_DATE defaults to today.
        TODO: formalize the defaults for JobFields via Job.__init__(Jobfields...
        """
        return {
            JobField.STATUS: JobStatus.NEW,
            JobField.LOCALE: self.locale,
            JobField.QUERY: self.config.search_config.query_string,
            JobField.DESCRIPTION: '',
            JobField.URL: '',
            JobField.SHORT_DESCRIPTION: '',
            JobField.RAW: None,
            JobField.PROVIDER: self.__class__.__name__,
            JobField.REMOTE: '',
            JobField.WAGE: '',
        }

    @property
    def min_required_job_fields(self) -> List[JobField]:
        """If we dont get() or set() any of these fields, we will raise an
        exception instead of continuing without that information.

        NOTE: pointless to check for locale / provider / other defaults

        Override if needed, but be aware that key_id should always be populated
        along with URL or the user can do nothing with the result.
        """
        return [
            JobField.TITLE, JobField.COMPANY, JobField.LOCATION,
            JobField.KEY_ID, JobField.URL
        ]

    @property
    @abstractmethod
    def job_get_fields(self) -> List[JobField]:
        """Call self.get(...) for the JobFields in this list when scraping a Job.

        NOTE: these will be passed job listing soups, if you have data you need
        to populate that exists in the Job.RAW (the soup from the listing's own
        page), you should use job_set_fields.
        """
        pass

    @property
    @abstractmethod
    def job_set_fields(self) -> List[JobField]:
        """Call self.set(...) for the JobFields in this list when scraping a Job

        NOTE: You should generally set the job's own page as soup to RAW first
        and then populate other fields from this soup, or from each-other here.
        """
        pass

    @property
    @abstractmethod
    def delayed_get_set_fields(self) -> List[JobField]:
        """Delay execution when getting /setting any of these attributes of a
        job.

        TODO: handle this within an overridden self.session.get()
        """
        pass

    @property
    def high_priority_get_set_fields(self) -> List[JobField]:
        """These get() and/or set() fields will be populated first.

        i.e we need the RAW populated before DESCRIPTION, so RAW should be high.
        i.e. we need to get key_id before we set job.url, so key_id is high.

        NOTE: override as needed.
        """
        return []

    @property
    @abstractmethod
    def locale(self) -> Locale:
        """The localization that this scraper was built for.

        i.e. I am looking for jobs on the Canadian version of Indeed, and I
        speak english, so I will have this return Locale.CANADA_ENGLISH

        We will use this to put the right filters & scrapers together

        NOTE: it is best to inherit this from Base<Locale>Class (btm. of file)
        """
        pass

    @property
    @abstractmethod
    def headers(self) -> Dict[str, str]:
        """The Session headers for this scraper to be used with
        requests.Session.headers.update()
        """
        pass

    def scrape(self) -> Dict[str, Job]:
        """Scrape job source into a dict of unique jobs keyed by ID

        Returns:
            jobs (Dict[str, Job]): list of Jobs in a Dict keyed by job.key_id
        """

        # Get a list of job soups from the initial search results page
        # These wont contain enough information to do more than initialize Job
        try:
            job_soups = self.get_job_soups_from_search_result_listings()
        except Exception as err:
            raise ValueError(
                "Unable to extract jobs from initial search result page:\n\t"
                f"{str(err)}"
            )
        n_soups = len(job_soups)
        self.logger.info(
            f"Scraped {n_soups} job listings from search results pages"
        )

        # Init a Manager so we can control delaying
        # TODO: make session use async io to coordinate on-the-fly delaying.
        # this is assuming every job will incur one delayed session.get()
        # NOTE pylint issue: https://github.com/PyCQA/pylint/issues/3313
        delay_lock = self.thread_manager.Lock()  # pylint: disable=no-member
        threads = ThreadPoolExecutor(max_workers=MAX_CPU_WORKERS)

        # Distribute work to N workers such that each worker is building one
        # Job at a time, getting and setting all required attributes
        jobs_dict = {}  # type: Dict[str, Job]
        try:
            # Calculate delays for get/set calls per-job NOTE: only get/set
            # calls in self.delayed_get_set_fields will be delayed.
            # and it busy-waits.
            delays = calculate_delays(n_soups, self.config.delay_config)
            futures = []
            for job_soup, delay in zip(job_soups, delays):
                futures.append(
                    threads.submit(
                        self.scrape_job,
                        job_soup=job_soup,
                        delay=delay,
                        delay_lock=delay_lock,
                    )
                )

            # Loops through futures as completed and removes if successfully parsed
            # For each job-soup object, scrape the soup into a Job  (w/o desc.)
            for future in tqdm(as_completed(futures), total=n_soups):
                job = future.result()
                if job:
                    # Handle duplicates that exist within the scraped data itself.
                    # NOTE: if you see alot of these our scrape for key_id is bad
                    if job.key_id in jobs_dict:
                        self.logger.error(
                            f"Job {job.title} and {jobs_dict[job.key_id].title} "
                            f"share duplicate key_id: {job.key_id}"
                        )
                    jobs_dict[job.key_id] = job

        finally:
            # Cleanup
            threads.shutdown()

        return jobs_dict

    # pylint: disable=no-member
    def scrape_job(self, job_soup: BeautifulSoup, delay: float,
                   delay_lock: Optional[Lock] = None) -> Optional[Job]:
        """Scrapes a search page and get a list of soups that will yield jobs
        Arguments:
            job_soup (BeautifulSoup): This is a soup object that your get/set
                will use to perform the get/set action. It should be specific
                to this job and not contain other job information.
            delay (float): how long to delay getting/setting for certain
                get/set calls while scraping data for this job.
            delay_lock (Optional[Manager.Lock], optional): semaphore for
                synchronizing respectful delaying across workers

        NOTE: this will never raise an exception to prevent killing workers,
            who are building jobs sequentially.

        Returns:
            Optional[Job]: job object constructed from the soup and localization
                of class, returns None if scrape failed.
        """
        # Scrape the data for the post, requiring a minimum of info...
        # NOTE: if we perform a self.session.get we may get respectfully delayed
        job = None  # type: Optional[Job]
        job_init_kwargs = self.job_init_kwargs  # NOTE: faster?
        for is_get, field in self._actions_list:

            # Break out immediately because we have failed a filterable
            # condition with something we initialized while scraping.
            # NOTE: if we pre-empt scraping duplicates we cannot update
            # the existing job listing with the new information!
            # TODO: make this configurable?
            if job and self.job_filter.filterable(job):
                if self.job_filter.is_duplicate(job):
                    # FIXME: make this configurable
                    self.logger.debug(
                        f"Scraped job {job.key_id} has key_id "
                        "in known duplicates list. Continuing scrape of job "
                        "to update existing job attributes."
                    )
                else:
                    self.logger.debug(
                        f"Cancelled scraping of {job.key_id}, failed JobFilter"
                    )  # TODO a reason would be nice maybe JobFilterFailure ?
                    break

            # Respectfully delay if it's configured to do so.
            # TODO: move into overriden session and manage this access there.
            if field in self.delayed_get_set_fields:
                if delay_lock:
                    self.logger.debug(f"Delaying for {delay}")
                    with delay_lock:
                        sleep(delay)
                else:
                    sleep(delay)

            try:
                if is_get:
                    job_init_kwargs[field] = self.get(field, job_soup)
                else:
                    if not job:
                        # Build initial job object + populate all the job
                        job = Job(**{
                            k.name.lower(): v for k, v
                            in job_init_kwargs.items()
                        })
                    self.set(field, job, job_soup)

            except Exception as err:

                if field in self.min_required_job_fields:
                    raise ValueError(
                        "Unable to scrape minimum-required job field: "
                        f"{field.name} Got error:{str(err)}"
                    )
                else:
                    # Crash out gracefully so we can continue scraping.
                    self.logger.warning(
                        f"Unable to scrape {field.name.lower()} for job:"
                        f"\n\t{str(err)}"
                    )
                # Log the job url if we have it.
                # TODO: we should really dump the soup object to an XML file
                # so that users encountering bugs can submit it and we can
                # quickly fix any failing scraping.
                if job.url:
                    self.logger.debug(f"Job URL was {job.url}")

        # Validate job fields if we got something
        if job:
            job.validate()

        return job
    # pylint: enable=no-member

    @abstractmethod
    def get_job_soups_from_search_result_listings(self) -> List[BeautifulSoup]:
        """Scrapes a job provider's response to a search query where we are
        shown many job listings at once.

        NOTE: the soups list returned by this method should contain enough
        information to set your self.min_required_job_fields with get()

        Returns:
            List[BeautifulSoup]: list of jobs soups we can use to make a Job
        """
        pass

    @abstractmethod
    def get(self, parameter: JobField, soup: BeautifulSoup) -> Any:
        """Get a single job attribute from a soup object by JobField

        i.e. if param is JobField.COMPANY --> scrape from soup --> return str
        TODO: better way to handle ret type?
        """
        pass

    @abstractmethod
    def set(self, parameter: JobField, job: Job, soup: BeautifulSoup) -> None:
        """Set a single job attribute from a soup object by JobField

        Use this to set Job attribs that rely on Job existing already
        with the required minimum fields.

        i.e. I can set() the Job.RAW to be the soup of it's own dedicated web
        page (Job.URL), then I can set() my Job.DESCRIPTION from the Job.RAW

        NOTE: (remember) do not return anything in here! it sets job attribs
        FIXME: have this automatically set the attribute by JobField.
        """
        pass

    def _validate_get_set(self) -> None:
        """Ensure the get/set actions cover all need attribs and dont intersect
        """
        set_job_get_fields = set(self.job_get_fields)
        set_job_set_fields = set(self.job_set_fields)
        all_set_get_fields = set(self.job_get_fields + self.job_set_fields)
        set_min_fields = set(self.min_required_job_fields)

        set_missing_req_fields = set_min_fields - all_set_get_fields
        if set_missing_req_fields:
            raise ValueError(
                f"Scraper: {self.__class__.__name__} Job attributes: "
                f"{set_missing_req_fields} are required and not implemented."
            )

        field_intersection = set_job_get_fields.intersection(set_job_set_fields)
        if field_intersection:
            raise ValueError(
                f"Scraper: {self.__class__.__name__} Job attributes: "
                f"{field_intersection} are implemented by both get() and set()!"
            )
        excluded_fields = []  # type: List[JobField]
        for field in JobField:
            # NOTE: we exclude status, locale, query, provider and scrape date
            # because these are set without needing any scrape data.
            # TODO: SHORT and RAW are not impl. rn. remove this check when impl.
            if (field not in [JobField.STATUS, JobField.LOCALE, JobField.QUERY,
                              JobField.SCRAPE_DATE, JobField.PROVIDER,
                              JobField.SHORT_DESCRIPTION, JobField.RAW]
                    and field not in self.job_get_fields
                    and field not in self.job_set_fields):
                        excluded_fields.append(field)
        if excluded_fields:
            # NOTE: INFO level because this is OK, but ideally ppl see this
            # so they are motivated to help and understand why stuff might
            # be missing in the CSV
            self.logger.info(
                "No get() or set() will be done for Job attrs: "
                f"{[field.name for field in excluded_fields]}"
            )


# Just some basic localized scrapers, you can inherit these to set the locale.
# TODO: move into own file once we get enough of em...
class BaseUSAEngScraper(BaseScraper):
    """Localized scraper for USA English
    """
    @property
    def locale(self) -> Locale:
        return Locale.USA_ENGLISH


class BaseCANEngScraper(BaseScraper):
    """Localized scraper for Canada English
    """
    @property
    def locale(self) -> Locale:
        return Locale.CANADA_ENGLISH
