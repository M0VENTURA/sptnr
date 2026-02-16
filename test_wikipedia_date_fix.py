#!/usr/bin/env python3
"""
Test to verify Wikipedia scraper correctly extracts year from source_key
"""
import unittest
from wikipedia_releases_scraper import WikipediaReleaseScraper


class TestWikipediaYearExtraction(unittest.TestCase):
    """Test year extraction from source keys"""
    
    def setUp(self):
        """Set up test scraper"""
        self.scraper = WikipediaReleaseScraper(db_path=":memory:")
    
    def test_extract_year_from_source_key_2026(self):
        """Test extraction of 2026 from source key"""
        year = self.scraper._extract_year_from_source_key("2026_albums")
        self.assertEqual(year, 2026)
    
    def test_extract_year_from_source_key_2027(self):
        """Test extraction of 2027 from source key"""
        year = self.scraper._extract_year_from_source_key("2027_rock")
        self.assertEqual(year, 2027)
    
    def test_extract_year_from_source_key_2025(self):
        """Test extraction of 2025 from source key"""
        year = self.scraper._extract_year_from_source_key("2025_heavy_metal")
        self.assertEqual(year, 2025)
    
    def test_extract_year_from_source_key_2030(self):
        """Test extraction of 2030 from source key"""
        year = self.scraper._extract_year_from_source_key("2030_kpop")
        self.assertEqual(year, 2030)
    
    def test_extract_year_from_source_key_default(self):
        """Test default year when no year in source key"""
        year = self.scraper._extract_year_from_source_key("albums_general")
        # Should return current year
        from datetime import datetime
        self.assertEqual(year, datetime.now().year)
    
    def test_parse_row_with_correct_year(self):
        """Test that _parse_row_for_month uses the year parameter"""
        from bs4 import BeautifulSoup
        
        # Create a mock row with artist and album
        html = '<td>5</td><td>Test Artist</td><td>Test Album</td>'
        soup = BeautifulSoup(html, 'html.parser')
        cells = soup.find_all('td')
        
        # Test with 2027 as the year
        result = self.scraper._parse_row_for_month(
            cells=cells,
            source_key="2027_albums",
            source_name="Test Source",
            current_month=3,  # March
            year=2027,
            column_order=['day', 'artist', 'album']
        )
        
        self.assertIsNotNone(result)
        self.assertEqual(result['release_year'], 2027)
        self.assertEqual(result['release_date'], '2027-03-05')
        self.assertEqual(result['artist_name'], 'Test Artist')
        self.assertEqual(result['album_name'], 'Test Album')
    
    def test_parse_row_with_2026(self):
        """Test that _parse_row_for_month works with 2026"""
        from bs4 import BeautifulSoup
        
        # Create a mock row with artist and album
        html = '<td>15</td><td>Another Artist</td><td>Another Album</td>'
        soup = BeautifulSoup(html, 'html.parser')
        cells = soup.find_all('td')
        
        result = self.scraper._parse_row_for_month(
            cells=cells,
            source_key="2026_rock",
            source_name="Test Source",
            current_month=6,  # June
            year=2026,
            column_order=['day', 'artist', 'album']
        )
        
        self.assertIsNotNone(result)
        self.assertEqual(result['release_year'], 2026)
        self.assertEqual(result['release_date'], '2026-06-15')
    
    def test_multiple_years_different_sources(self):
        """Test that different sources can have different years"""
        from bs4 import BeautifulSoup
        
        test_cases = [
            ("2025_albums", 2025, 1, 1, '2025-01-01'),
            ("2026_rock", 2026, 12, 25, '2026-12-25'),
            ("2027_metal", 2027, 6, 15, '2027-06-15'),
            ("2028_kpop", 2028, 3, 10, '2028-03-10'),
        ]
        
        for source_key, year, month, day, expected_date in test_cases:
            with self.subTest(source_key=source_key, year=year):
                html = f'<td>{day}</td><td>Artist {year}</td><td>Album {year}</td>'
                soup = BeautifulSoup(html, 'html.parser')
                cells = soup.find_all('td')
                
                result = self.scraper._parse_row_for_month(
                    cells=cells,
                    source_key=source_key,
                    source_name=f"Test {year}",
                    current_month=month,
                    year=year,
                    column_order=['day', 'artist', 'album']
                )
                
                self.assertIsNotNone(result)
                self.assertEqual(result['release_year'], year)
                self.assertEqual(result['release_date'], expected_date)
                self.assertEqual(result['artist_name'], f'Artist {year}')
                self.assertEqual(result['album_name'], f'Album {year}')


if __name__ == '__main__':
    unittest.main()
