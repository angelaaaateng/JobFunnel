"""Paul McInnis 2020
Scrapes jobs, applies search filters and writes pickles to master list
"""
import csv
import json
import logging
import os
import pickle
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from time import time
from typing import Dict, List, Optional

from requests import Session

from jobfunnel.backend import Job
from jobfunnel.backend.tools.filters import job_is_old, tfidf_filter
from jobfunnel.backend.tools import update_job_if_newer, get_logger
from jobfunnel.config import JobFunnelConfig
from jobfunnel.resources import (CSV_HEADER, MAX_BLOCK_LIST_DESC_CHARS,
                                 MAX_CPU_WORKERS, JobStatus, Locale,
                                 MIN_JOBS_TO_PERFORM_SIMILARITY_SEARCH)


class JobFunnel:
    """Class that initializes a Scraper and scrapes a website to get jobs
    """

    def __init__(self, config: JobFunnelConfig) -> None:
        """Initialize a JobFunnel object, with a JobFunnel Config

        Args:
            config (JobFunnelConfig): config object containing paths etc.
        """
        self.config = config
        self.config.create_dirs()
        self.config.validate()
        self.logger = get_logger(
            self.__class__.__name__,
            self.config.log_level,
            self.config.log_file,
            f"[%(asctime)s] [%(levelname)s] {self.__class__.__name__}: "
            "%(message)s"
        )
        self.__date_string = date.today().strftime("%Y-%m-%d")

        # Open a session with/out a proxy configured
        self.session = Session()
        if self.config.proxy_config:
            self.session.proxies = {
                self.config.proxy_config.protocol: self.config.proxy_config.url
            }

    @property
    def daily_cache_file(self) -> str:
        """The name for for pickle file containing the scraped data ran today'
        TODO: instead of using a 'daily' cache file, we should be tying this
        into the search that was made to prevent cross-caching results.
        """
        return os.path.join(
            self.config.cache_folder, f"jobs_{self.__date_string}.pkl",
        )

    def run(self) -> None:
        """Scrape, update lists and save to CSV.
        NOTE: we are assuming the user has distinct cache folder per-search,
        otherwise we will load the cache for today, for a different search!
        """
        # Load master csv jobs if they exist and update our block list with
        # any jobs the user has set the status to == a remove status
        # NOTE: we want to do this first to ensure scraping is efficient when
        # we are getting detailed job information (per-job)
        master_jobs_dict = {}  # type: Dict[str, Job[
        if os.path.isfile(self.config.master_csv_file):
            master_jobs_dict = self.read_master_csv()
            self.update_user_block_list(master_jobs_dict)
        else:
            logging.debug(
                "No master-CSV present, did not update block-list: "
                f"{self.config.user_block_list_file}"
            )

        # Get jobs keyed by their unique ID, use cache if --no-scrape is set
        scraped_jobs_dict = {}  # type: Dict[str, Job]
        if os.path.exists(self.daily_cache_file):
            scraped_jobs_dict = self.load_cache(self.daily_cache_file)
        elif self.config.no_scrape:
            self.logger.warning(
                f"No jobs cached, missing: {self.daily_cache_file}"
            )

        # Scrape and writeout the cache
        if self.config.no_scrape:
            self.logger.info("Skipping scraping, running with --no-scrape.")
        else:
            scraped_jobs_dict = self.scrape()  # type: Dict[str, Job]
            self.write_cache(scraped_jobs_dict)

        # Pre-filter by removing jobs with duplicate IDs from scraped_jobs_dict
        if master_jobs_dict:
            self.filter_duplicates(
                scraped_jobs_dict, master_jobs_dict, by_key_id_only=True,
            )

        # Filter out scraped jobs we have rejected, archived or block-listed
        # or which we previously detected to be duplicates before updating CSV.
        self.filter(scraped_jobs_dict)

        # Update master CSV iif we have one
        if master_jobs_dict:

            # Mabye reduce the size of master_jobs (may have blocked new jobs)
            self.filter(master_jobs_dict)

            # Filter out duplicates and update duplicates list file
            # NOTE: this will match duplicates by job description contents
            self.filter_duplicates(scraped_jobs_dict, master_jobs_dict)

            # Expand master_jobs_dict with filtered, non-duplicated jobs & save
            # NOTE: this may be an empty update.. TODO: save the write call?
            master_jobs_dict.update(scraped_jobs_dict)
            self.write_master_csv(master_jobs_dict)

        else:
            # Dump the results into the data folder as the masterlist
            # FIXME: we could still detect duplicates within the CSV itself?
            self.write_master_csv(scraped_jobs_dict)

        self.logger.info(
            f"Done. View your current jobs in {self.config.master_csv_file}"
        )

    def scrape(self) ->Dict[str, Job]:
        """Run each of the desired Scraper.scrape() with threading and delaying
        """
        self.logger.info(
            f"Scraping local providers with: {self.config.scraper_names}"
        )

        # Iterate thru scrapers and run their scrape.
        jobs = {}  # type: Dict[str, Job]
        for scraper_cls in self.config.scrapers:
            # FIXME: need to add the threader and delaying here
            start = time()
            scraper = scraper_cls(self.session, self.config)
            # TODO: add a warning for overwriting different jobs with same key
            jobs.update(scraper.scrape())
            end = time()
            self.logger.debug(
                f"Scraped {len(jobs.items())} jobs from {scraper_cls.__name__},"
                f" took {(end - start):.3f}s"
            )

        self.logger.info(f"Completed all scraping, found {len(jobs)} new jobs.")
        return jobs

    def recover(self) -> None:
        """Build a new master CSV from all the available pickles in our cache
        """
        self.logger.info("Recovering jobs from all cache files in cache folder")
        if os.path.exists(self.config.user_block_list_file):
            self.logger.warning(
                "Running recovery mode, but with existing block-list, delete "
                f"{self.config.user_block_list_file} if you want to start fresh"
                " from the cached data and not filter any jobs away."
            )
        all_jobs_dict = {}
        for file in os.listdir(self.config.cache_folder):
            if '.pkl' in file:
                all_jobs_dict.update(
                    self.load_cache(
                        os.path.join(self.config.cache_folder, file)
                    )
                )
        self.filter(all_jobs_dict)
        self.write_master_csv(all_jobs_dict)

    def load_cache(self, cache_file: str) -> Dict[str, Job]:

        """Load today's scrape data from pickle via date string

        TODO: search the cache for pickles that match search config.
        (we may need a registry for the pickles and seach terms used)

        Args:
            cache_file (str): path to cache pickle file containing jobs dict
                keyed by Job.KEY_ID.

        Raises:
            FileNotFoundError: if cache file is missing

        Returns:
            Dict[str, Job]: [description]
        """
        if not os.path.exists(cache_file):
            raise FileNotFoundError(
                f"{cache_file} not found! Have you scraped any jobs today?"
            )
        else:
            jobs_dict = pickle.load(open(cache_file, 'rb'))
            self.logger.info(
                f"Read {len(jobs_dict.keys())} jobs from previously-scraped "
                f"jobs cache: {cache_file}."
            )
            self.logger.debug(
                "NOTE: you may see many duplicate IDs detected if these jobs "
                "exist in your master CSV already."
            )
            return jobs_dict

    def write_cache(self, jobs_dict: Dict[str, Job],
                    cache_file: str = None) -> None:
        """Dump a jobs_dict into a pickle

        TODO: write search_config into the cache file and jobfunnel version
        FIXME: some way to cache raw data without recur-limit

        Args:
            jobs_dict (Dict[str, Job]): jobs dict to dump into cache.
            cache_file (str, optional): file path to write to. Defaults to None.
        """

        cache_file = cache_file if cache_file else self.daily_cache_file
        pickle.dump(jobs_dict, open(cache_file, 'wb'))
        self.logger.debug(
            f"Dumped {len(jobs_dict.keys())} jobs to {cache_file}"
        )

    def read_master_csv(self) -> Dict[str, Job]:
        """Read in the master-list CSV to a dict of unique Jobs

        TODO: update from legacy CSV header for short & long description
        TODO: the header contents should match JobField names

        Returns:
            Dict[str, Job]: unique Job objects in the CSV
        """
        jobs_dict = {}  # type: Dict[str, Job]
        with open(self.config.master_csv_file, 'r', encoding='utf8',
                  errors='ignore') as csvfile:
            for row in csv.DictReader(csvfile):
                # NOTE: we are doing legacy support here with 'blurb' etc.
                if 'description' in row:
                    short_description = row['description']
                else:
                    short_description = ''
                post_date = datetime.strptime(row['date'], '%Y-%m-%d')
                if 'scrape_date' in row:
                    scrape_date = datetime.strptime(
                        row['scrape_date'], '%Y-%m-%d'
                    )
                else:
                    scrape_date = post_date
                if 'raw' in row:
                    raw = row['raw']
                else:
                    raw = None

                # We need to convert from user statuses
                # TODO: put this in Job?
                status = None
                if 'status' in row:
                    status_str = row['status'].strip()
                    for p_status in JobStatus:
                        if status_str.lower() == p_status.name.lower():
                            status = p_status
                            break
                if not status:
                    self.logger.warning(
                        f"Unknown status {status_str}, setting to UNKNOWN"
                    )
                    status = JobStatus.UNKNOWN

                # NOTE: this is for legacy support:
                locale = None
                if 'locale' in row:
                    locale_str = row['locale'].strip()
                    for p_locale in Locale:
                        if locale_str.lower() == p_locale.name.lower():
                            locale = p_locale
                            break
                if not locale:
                    self.logger.warning(
                        f"Unknown locale {locale_str}, setting to UNKNOWN"
                    )
                    locale = locale.UNKNOWN

                job = Job(
                    title=row['title'],
                    company=row['company'],
                    location=row['location'],
                    description=row['blurb'],
                    key_id=row['id'],
                    url=row['link'],
                    locale=locale,
                    query=row['query'],
                    status=status,
                    provider=row['provider'],
                    short_description=short_description,
                    post_date=post_date,
                    scrape_date=scrape_date,
                    raw=raw,
                    tags=row['tags'].split(','),
                )
                job.validate()
                jobs_dict[job.key_id] = job

        self.logger.debug(
            f"Read {len(jobs_dict.keys())} jobs from master-CSV: "
            f"{self.config.master_csv_file}"
        )
        return jobs_dict

    def write_master_csv(self, jobs: Dict[str, Job]) -> None:
        """Write out our dict of unique Jobs to a CSV

        Args:
            jobs (Dict[str, Job]): Dict of unique Jobs, keyd by unique id's
        """
        with open(self.config.master_csv_file, 'w', encoding='utf8') as csvfile:
            writer = csv.DictWriter(csvfile, fieldnames=CSV_HEADER)
            writer.writeheader()
            for job in jobs.values():
                job.validate()
                writer.writerow(job.as_row)
        self.logger.debug(
            f"Wrote {len(jobs)} jobs to {self.config.master_csv_file}"
        )

    def update_user_block_list(self,
                               master_jobs_dict: Optional[Dict[str, Job]] = None
                               ) -> None:
        """From data in master CSV file, add jobs with removeable statuses to
        our configured user block list file and save (if any)

        NOTE: we assume that the contents of master_jobs_dict match the contents
        returned by self.read_master_csv, passing this argument just saves us
        loading twice in jobfunnel.run()

        NOTE: adding jobs to block list will result in filter() removing them
        from all scraped & cached jobs in the future.

        Args:
            master_jobs_dict (Optional[Dict[str, Job]], optional): the existing
                jobs in the user's master CSV file. If None we will load from
                CSV or raise an error if CSV does not exist.
        Raises:
            FileNotFoundError: if no master_jobs_dict is provided and master csv
                               file does not exist.
        """

        # Load from CSV if not passed by argument
        if not master_jobs_dict:
            if os.path.isfile(self.config.master_csv_file):
                master_jobs_dict or self.read_master_csv()
            else:
                raise FileNotFoundError(
                    f"Cannot update {self.config.user_block_list_file} without "
                    f"{self.config.master_csv_file}"
                )

        # Load existing filtered jobs, if any
        if os.path.isfile(self.config.user_block_list_file):
            blocked_jobs_dict = json.load(
                open(self.config.user_block_list_file, 'r')
            )
        else:
            blocked_jobs_dict = {}

        # Add jobs from csv that need to be filtered away, if any
        n_jobs_added = 0
        for job in master_jobs_dict.values():
            if job.is_remove_status and job.key_id not in blocked_jobs_dict:
                n_jobs_added += 1
                blocked_jobs_dict[job.key_id] = job.as_json_entry
                logging.info(
                    f'Added {job.key_id} to '
                    f'{self.config.user_block_list_file}'
                )

        if n_jobs_added:
            # Write out complete list with any additions from the masterlist
            # NOTE: we use indent=4 so that it stays human-readable.
            with open(self.config.user_block_list_file, 'w',
                      encoding='utf8') as outfile:
                outfile.write(
                    json.dumps(
                        blocked_jobs_dict,
                        indent=4,
                        sort_keys=True,
                        separators=(',', ': '),
                        ensure_ascii=False,
                    )
                )
            self.logger.info(
                f"Moved {n_jobs_added} jobs into block-list due to removable "
                f"statuses: {self.config.user_block_list_file}"
            )

    def filter(self, jobs_dict: Dict[str, Job]) -> int:
        """Remove jobs from jobs_dict if they are:
            1. in our block-list
            2. status == a removal status string (i.e. DELETE)
            3. job.company == one of our blocked company names

        Returns the number of filtered jobs

        NOTE: this also removes any duplicates from jobs_dict if a duplicates
        list file is configured.

        TODO: make the filters used configurable, i.e. list of FilterType
        """
        # Read the user's block list
        block_dict = {}  # type: Dict[str, Job]
        if os.path.isfile(self.config.user_block_list_file):
            block_dict = json.load(
                open(self.config.user_block_list_file, 'r')
            )

        # Read the user's duplicate jobs list (from TFIDF)
        duplicates_dict = {}  # type: Dict[str, Job]
        if os.path.isfile(self.config.duplicates_list_file):
            duplicates_dict = json.load(
                open(self.config.user_block_list_file, 'r')
            )

        # Filter jobs out using all our available filters
        # NOTE: checks are arranged in order of assumed calculation expense
        filter_jobs_ids = []
        for key_id, job in jobs_dict.items():
            if (job.is_remove_status
                or job.company in
                self.config.search_config.blocked_company_names
                or key_id in block_dict
                or key_id in duplicates_dict
                or job_is_old(job, self.config.search_config.max_listing_days)):
                filter_jobs_ids.append(key_id)

        for key_id in filter_jobs_ids:
            jobs_dict.pop(key_id)

        n_filtered = len(filter_jobs_ids)
        if n_filtered > 0:
            self.logger.info(
                f"Removed {n_filtered} job(s) from scraped data, jobs are "
                "blocked/removed, old, or content-duplicates of jobs in "
                "master CSV."
            )

        return n_filtered


    def filter_duplicates(self, scraped_jobs_dict: Dict[str, Job],
                          existing_jobs_dict: Dict[str, Job],
                          by_key_id_only: bool = False) -> None:
        """Identify duplicate jobs between scrape data and existing_jobs_dict
        and update the duplicates block list if any are found by contents.

        TODO: move this into self.filter() which should be more configurable
        TODO: make max_similarity configurable i.e. self.config.filter...
        TODO: we are wrapping in a try/catch because TFIDF filter is missing
              some error handling. Remove once it is safer to use w.out crashing
        NOTE: only duplicates detected by job contents will be written to
              the duplicates_list_file JSON, as checking by key_id is not
              an expensive comparison vs full TFIDF vectorization.
        NOTE: when we detect that an existing job is a duplicate of a new job
              we update the existing job with the new job's post date and other
              information. (only if post date is newer!)

        Args:
            scraped_jobs_dict (Dict[str, Job]): currently scraped jobs dict
            existing_jobs_dict (Dict[str, Job]): existing jobs dict i.e. master
            by_key_id_only (bool, optional): if True, only remove duplicates
                via key_id. If false, use the contents of the jobs to identify
                duplicates as well (NOTE: currently only TFIDF filter for desc).
        """
        # First we need to remove any duplicates by id directly
        for key_id in existing_jobs_dict:
            if key_id in scraped_jobs_dict:
                duplicate_job = scraped_jobs_dict.pop(key_id)
                if update_job_if_newer(existing_jobs_dict[key_id],
                                       duplicate_job):
                    self.logger.debug(
                        f"Updated job {key_id} with duplicate's contents."
                    )

        # If we have any jobs left, filter these using their contents.
        if scraped_jobs_dict:
            if (len(scraped_jobs_dict.keys()) + len(existing_jobs_dict.keys())
                    >= MIN_JOBS_TO_PERFORM_SIMILARITY_SEARCH):
                try:
                    tfidf_filter(
                        cur_dict=scraped_jobs_dict,
                        prev_dict=existing_jobs_dict,
                        log_level=self.config.log_level,
                        log_file=self.config.log_file,
                        duplicate_jobs_file=self.config.duplicates_list_file,
                    )
                except ValueError as err:
                    self.logger.error(
                        f"Skipping similarity filter due to error: {str(err)}"
                    )
            else:
                self.logger.warning(
                    "Skipping similarity filter because there are fewer than "
                    f"{MIN_JOBS_TO_PERFORM_SIMILARITY_SEARCH} jobs."
                )