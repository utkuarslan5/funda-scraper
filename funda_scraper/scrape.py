"""Main funda scraper module"""
import argparse
import datetime
import json
import asyncio
import random
import os
from typing import List, Optional

import pandas as pd
import aiofiles
import aiohttp
import requests
from io import StringIO
from bs4 import BeautifulSoup
from tqdm.asyncio import tqdm as atqdm
from tqdm import tqdm
from tqdm.contrib.concurrent import process_map

from funda_scraper.config.core import config
from funda_scraper.preprocess import clean_date_format, async_preprocess_data
from funda_scraper.utils import logger


class FundaScraper(object):
    """
    Handles the main scraping function.
    """
    def __init__(
        self,
        area: str,
        want_to: str,
        page_start: int = 1,
        n_pages: int = 1,
        find_past: bool = False,
        min_price: Optional[int] = None,
        max_price: Optional[int] = None,
        days_since: Optional[int] = None,
        property_type: Optional[str] = None,
    ):
        # Init attributes
        self.area = area.lower().replace(" ", "-")
        self.property_type = property_type
        self.want_to = want_to
        self.find_past = find_past
        self.page_start = max(page_start, 1)
        self.n_pages = max(n_pages, 1)
        self.page_end = self.page_start + self.n_pages - 1
        self.min_price = min_price
        self.max_price = max_price
        self.days_since = days_since

        # Instantiate along the way
        self.links: List[str] = []
        self.raw_df = pd.DataFrame()
        self.clean_df = pd.DataFrame()
        self.base_url = config.base_url
        self.selectors = config.css_selector

    def __repr__(self):
        return (f"FundaScraper(area={self.area}, "
            f"want_to={self.want_to}, "
            f"n_pages={self.n_pages}, "
            f"page_start={self.page_start}, "
            f"find_past={self.find_past}, " 
            f"min_price={self.min_price}, "
            f"max_price={self.max_price}, "
            f"days_since={self.days_since})")

    def reset(
        self,
        area: Optional[str] = None,
        property_type: Optional[str] = None,
        want_to: Optional[str] = None,
        page_start: Optional[int] = None,
        n_pages: Optional[int] = None,
        find_past: Optional[bool] = None,
        min_price: Optional[int] = None,
        max_price: Optional[int] = None,
        days_since: Optional[int] = None,
    ) -> None:
        """Overwrite or initialise the searching scope."""
        if area is not None:
            self.area = area
        if property_type is not None:
            self.property_type = property_type
        if want_to is not None:
            self.want_to = want_to
        if page_start is not None:
            self.page_start = max(page_start, 1)
        if n_pages is not None:
            self.n_pages = max(n_pages, 1)
        if find_past is not None:
            self.find_past = find_past
        if min_price is not None:
            self.min_price = min_price
        if max_price is not None:
            self.max_price = max_price
        if days_since is not None:
            self.days_since = days_since

    @property
    def to_buy(self) -> bool:
        """Whether to buy or not"""
        if self.want_to.lower() in ["buy", "koop", "b", "k"]:
            return True
        elif self.want_to.lower() in ["rent", "huur", "r", "h"]:
            return False
        else:
            raise ValueError("'want_to' must be either 'buy' or 'rent'.")

    @property
    def check_days_since(self) -> int:
        """Whether days since complies"""
        if self.find_past:
            raise ValueError("'days_since' can only be specified when find_past=False.")

        if self.days_since in [None, 1, 3, 5, 10, 30]:
            return self.days_since
        else:
            raise ValueError("'days_since' must be either None, 1, 3, 5, 10 or 30.")

    @staticmethod
    def _check_dir() -> None:
        """Check whether a temporary directory for data"""
        if not os.path.exists("data"):
            os.makedirs("data")
    
    def _build_main_query_url(self) -> str:
        query = "koop" if self.to_buy else "huur"

        main_url = (
            f"{self.base_url}/zoeken/{query}?selected_area=%5B%22{self.area}%22%5D"
        )

        if self.property_type:
            property_types = self.property_type.split(",")
            formatted_property_types = [
                "%22" + prop_type + "%22" for prop_type in property_types
            ]
            main_url += f"&object_type=%5B{','.join(formatted_property_types)}%5D"

        if self.find_past:
            main_url = f"{main_url}&availability=%22unavailable%22"

        if self.min_price is not None or self.max_price is not None:
            min_price = "" if self.min_price is None else self.min_price
            max_price = "" if self.max_price is None else self.max_price
            main_url = f"{main_url}&price=%22{min_price}-{max_price}%22"

        if self.days_since is not None:
            main_url = f"{main_url}&publication_date={self.check_days_since}"
        logger.info(f"*** Main URL: {main_url} ***")
        return main_url

    @staticmethod
    def get_value_from_css(soup: BeautifulSoup, selector: str) -> str:
        """Use CSS selector to find certain features."""
        result = soup.select(selector)
        if len(result) > 0:
            result = result[0].text
        else:
            result = "na"
        return result

    @staticmethod
    async def _get_links_from_one_parent(url: str) -> List[str]:
        """Scrape all the available housing items from one Funda search page."""
        try:
            async with aiohttp.ClientSession(headers=config.header) as session:
                async with session.get(url) as response:
                    if response.status != 200:
                        logger.error(f"Failed to fetch {url}: HTTP {response.status}")
                        return []
                    response_text = await response.text()
                    
                    # Introduce a random delay
                    await asyncio.sleep(random.uniform(0.5, 2))

            soup = BeautifulSoup(response_text, "lxml")
            script_tags = soup.find_all("script", {"type": "application/ld+json"})
            if not script_tags:
                logger.warning(f"No script tags found in {url}")
                return []

            json_data = json.loads(script_tags[0].contents[0])
            urls = [item["url"] for item in json_data["itemListElement"]]
            return list(set(urls))

        except Exception as e:
            logger.error(f"Error fetching links from {url}: {e}")
            return []


    async def fetch_all_links(self, page_start: int = None, n_pages: int = None) -> None:
        """Find all the available links across multiple pages asynchronously."""

        page_start = self.page_start if page_start is None else page_start
        n_pages = self.n_pages if n_pages is None else n_pages

        logger.info("*** Phase 1: Fetch all the available links from all pages *** ")
        main_url = self._build_main_query_url()

        tasks = []
        for i in range(page_start, page_start + n_pages):
            url = f"{main_url}&search_result={i}"
            tasks.append(self._get_links_from_one_parent(url))

        urls = []
        async for item_list in atqdm(asyncio.as_completed(tasks), total=len(tasks), desc="Fetching Links"):
            try:
                urls += await item_list
            except IndexError:
                self.page_end = i
                logger.info(f"*** The last available page is {self.page_end} ***")
                break

        urls = list(set(urls))
        logger.info(f"*** Got all the urls. {len(urls)} houses found from {self.page_start} to {self.page_end} ***")
        self.links = urls


    async def scrape_one_link(self, link: str) -> List[str]:
        """Scrape all the features from one house item given a link."""
        try:
            async with aiohttp.ClientSession(headers=config.header) as session:
                async with session.get(link) as response:
                    if response.status != 200:
                        logger.error(f"Failed to fetch {link}: HTTP {response.status}")
                        return []
                    response_text = await response.text()

            soup = BeautifulSoup(response_text, "lxml")

            # Get the value according to respective CSS selectors
            if self.to_buy:
                if self.find_past:
                    list_since_selector = self.selectors.date_list
                else:
                    list_since_selector = self.selectors.listed_since
            else:
                if self.find_past:
                    list_since_selector = ".fd-align-items-center:nth-child(9) span"
                else:
                    list_since_selector = ".fd-align-items-center:nth-child(7) span"

            result = [
                link,
                self.get_value_from_css(soup, self.selectors.price),
                self.get_value_from_css(soup, self.selectors.address),
                self.get_value_from_css(soup, self.selectors.descrip),
                self.get_value_from_css(soup, list_since_selector),
                self.get_value_from_css(soup, self.selectors.zip_code),
                self.get_value_from_css(soup, self.selectors.size),
                self.get_value_from_css(soup, self.selectors.year),
                self.get_value_from_css(soup, self.selectors.living_area),
                self.get_value_from_css(soup, self.selectors.kind_of_house),
                self.get_value_from_css(soup, self.selectors.building_type),
                self.get_value_from_css(soup, self.selectors.num_of_rooms),
                self.get_value_from_css(soup, self.selectors.num_of_bathrooms),
                self.get_value_from_css(soup, self.selectors.layout),
                self.get_value_from_css(soup, self.selectors.energy_label),
                self.get_value_from_css(soup, self.selectors.insulation),
                self.get_value_from_css(soup, self.selectors.heating),
                self.get_value_from_css(soup, self.selectors.ownership),
                self.get_value_from_css(soup, self.selectors.exteriors),
                self.get_value_from_css(soup, self.selectors.parking),
                self.get_value_from_css(soup, self.selectors.neighborhood_name),
                self.get_value_from_css(soup, self.selectors.date_list),
                self.get_value_from_css(soup, self.selectors.date_sold),
                self.get_value_from_css(soup, self.selectors.term),
                self.get_value_from_css(soup, self.selectors.price_sold),
                self.get_value_from_css(soup, self.selectors.last_ask_price),
                self.get_value_from_css(soup, self.selectors.last_ask_price_m2).split("\r")[
                    0
                ],
            ]

            # Deal with list_since_selector especially, since its CSS varies sometimes
            if clean_date_format(result[4]) == "na":
                for i in range(6, 16):
                    selector = f".fd-align-items-center:nth-child({i}) span"
                    update_list_since = self.get_value_from_css(soup, selector)
                    if clean_date_format(update_list_since) == "na":
                        pass
                    else:
                        result[4] = update_list_since

            photos_list = [
                p.get("data-lazy-srcset") for p in soup.select(self.selectors.photo)
            ]
            photos_string = ", ".join(photos_list)

            # Clean up the retried result from one page
            result = [r.replace("\n", "").replace("\r", "").strip() for r in result]
            result.append(photos_string)
            return result
        except Exception as e:
            logger.error(f"Error scraping {link}: {e}")
            return None

    async def scrape_pages(self) -> None:
        """Scrape all the content acoss multiple pages."""

        logger.info("*** Phase 2: Start scraping from individual links ***")
        df = pd.DataFrame({key: [] for key in self.selectors.keys()})

        # Creating async tasks for each link
        scrape_tasks = [self.scrape_one_link(link) for link in self.links]
        content = await asyncio.gather(*scrape_tasks)
        
        for i, c in enumerate(content):
            df.loc[len(df)] = c
            
        df["city"] = df["url"].map(lambda x: x.split("/")[4])
        df["log_id"] = datetime.datetime.now().strftime("%Y%m-%d%H-%M%S")
        if not self.find_past:
            df = df.drop(["term", "price_sold", "date_sold"], axis=1)
        logger.info(f"*** All scraping done: {df.shape[0]} results ***")
        self.raw_df = df

    def save_csv(self, df: pd.DataFrame, filepath: str = None) -> None:
        """Save the result to a .csv file."""
        if filepath is None:
            self._check_dir()
            date = str(datetime.datetime.now().date()).replace("-", "")
            status = "unavailable" if self.find_past else "unavailable"
            want_to = "buy" if self.to_buy else "rent"
            filepath = f"./data/houseprice_{date}_{self.area}_{want_to}_{status}_{len(self.links)}.csv"
        df.to_csv(filepath, index=False)
        logger.info(f"*** File saved: {filepath}. ***")

    async def run(
        self, raw_data: bool = False, save: bool = False, filepath: str = None
    ) -> pd.DataFrame:
        """
        Scrape all links and all content.

        :param raw_data: if true, the data won't be pre-processed
        :param save: if true, the data will be saved as a csv file
        :param filepath: the name for the file
        :return: the (pre-processed) dataframe from scraping
        """
        await self.fetch_all_links()
        await self.scrape_pages()

        if raw_data:
            df = self.raw_df
        else:
            logger.info("*** Cleaning data ***")
            df = await async_preprocess_data(df=self.raw_df, is_past=self.find_past)
            self.clean_df = df

        if save:
            self.save_csv(df, filepath)

        logger.info("*** Done! ***")
        return df

async def main():
    scraper = FundaScraper(
        area=args.area,
        want_to=args.want_to,
        find_past=args.find_past,
        page_start=args.page_start,
        n_pages=args.n_pages,
        min_price=args.min_price,
        max_price=args.max_price,
        days_since=args.days_since,
    )

    df = await scraper.run(raw_data=args.raw_data, save=args.save)
    print(df.head())
    
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--area",
        type=str,
        help="Specify which area you are looking for",
        default="amsterdam",
    )
    parser.add_argument(
        "--want_to",
        type=str,
        help="Specify you want to 'rent' or 'buy'",
        default="rent",
    )
    parser.add_argument(
        "--find_past",
        type=bool,
        help="Indicate whether you want to use hisotrical data or not",
        default=False,
    )
    parser.add_argument(
        "--page_start", type=int, help="Specify which page to start scraping", default=1
    )
    parser.add_argument(
        "--n_pages", type=int, help="Specify how many pages to scrape", default=1
    )
    parser.add_argument(
        "--min_price", type=int, help="Specify the min price", default=None
    )
    parser.add_argument(
        "--max_price", type=int, help="Specify the max price", default=None
    )
    parser.add_argument(
        "--days_since",
        type=int,
        help="Specify the days since publication",
        default=None,
    )
    parser.add_argument(
        "--raw_data",
        type=bool,
        help="Indicate whether you want the raw scraping result or preprocessed one",
        default=False,
    )
    parser.add_argument(
        "--save",
        type=bool,
        help="Indicate whether you want to save the data or not",
        default=True,
    )

    args = parser.parse_args()
    scraper = FundaScraper(
        area=args.area,
        want_to=args.want_to,
        find_past=args.find_past,
        page_start=args.page_start,
        n_pages=args.n_pages,
        min_price=args.min_price,
        max_price=args.max_price,
        days_since=args.days_since,
    )
    # Run the scraper within an async context
    asyncio.run(main())
